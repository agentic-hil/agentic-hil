"""What the ESP32 bench stage writes and reads, checked where there is no board.

The stage runs only on the bench, where a configuration it could not write, or
a pattern that never matches what the ROM prints, fails the session after a
board was handed in. Whether each control-line setting's variant is a
configuration the product loads, with one esptool debugger and the bridge's
port it names, and whether the stage tells the line the ROM prints on a reset
from an image's, are questions this host answers without one. The pattern for
the images' own lines is held against the images in
``test_bench_esp32_images.py``, which reads them and so does not ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import write_config

from agentic_hil.config import load_config
from tests.bench.esp32_recordings import (
    CONSOLE_BAUDRATE,
    ESP32,
    LINE_SETTINGS,
    ROM_RESET,
    TICK_LINE,
    esp32_variant,
)

CH340 = {"vid": 0x1A86, "pid": 0x7523}


def loaded_variant(tmp_path: Path, *, asserted: bool, serial_number: str | None = None):
    session = write_config(tmp_path / "project", config_version=3)
    document = yaml.safe_load(session.read_text(encoding="utf-8"))
    variant = esp32_variant(document, device="/dev/ttyUSB9", serial_number=serial_number, flash_address="0x1000", asserted=asserted, **CH340)
    written = tmp_path / "esp32" / session.name
    written.parent.mkdir(parents=True)
    written.write_text(yaml.safe_dump(variant, sort_keys=False), encoding="utf-8")
    return variant, load_config(str(written))


@pytest.mark.parametrize(("setting", "asserted"), list(LINE_SETTINGS.items()))
def test_each_line_setting_loads_as_one_esptool_debugger_on_the_bridge_port(tmp_path: Path, setting: str, asserted: bool) -> None:
    _, config = loaded_variant(tmp_path, asserted=asserted)
    assert list(config.debuggers) == [ESP32]
    debugger = config.debuggers[ESP32]
    assert (debugger.type, debugger.com_port, debugger.target_type, debugger.flash_address) == ("esptool", ESP32, ESP32, "0x1000")
    assert debugger.probe_id is None
    assert debugger.permissions.allow_flash and debugger.permissions.allow_reset
    assert not debugger.permissions.allow_raw_debugger_commands and not debugger.permissions.allow_mass_erase
    assert list(config.com_ports) == [ESP32]
    port = config.com_ports[ESP32]
    assert (port.device, port.vid, port.pid, port.baudrate) == ("/dev/ttyUSB9", CH340["vid"], CH340["pid"], CONSOLE_BAUDRATE)
    assert (port.assert_dtr, port.assert_rts) == (asserted, asserted), setting
    assert port.permissions.allow_write is False
    assert config.can_buses == {}


def test_a_bridge_without_a_serial_number_is_declared_as_identified_by_its_usb_ids(tmp_path: Path) -> None:
    variant, config = loaded_variant(tmp_path, asserted=True)
    assert variant["com_ports"][ESP32]["identity_source"] == "vid_pid"
    assert config.com_ports[ESP32].serial_number is None
    assert config.com_ports[ESP32].identity_source == "vid_pid"


def test_a_bridge_with_a_serial_number_is_identified_by_it_and_declares_nothing(tmp_path: Path) -> None:
    variant, config = loaded_variant(tmp_path, asserted=True, serial_number="0001")
    assert "identity_source" not in variant["com_ports"][ESP32]
    assert config.com_ports[ESP32].serial_number == "0001"
    assert config.com_ports[ESP32].identity_source is None


def test_the_variant_leaves_the_session_configuration_it_was_built_from_alone(tmp_path: Path) -> None:
    session = write_config(tmp_path / "project", config_version=3)
    document = yaml.safe_load(session.read_text(encoding="utf-8"))
    before = yaml.safe_dump(document, sort_keys=True)
    variant = esp32_variant(document, device="/dev/ttyUSB9", serial_number=None, flash_address="0x1000", asserted=False, **CH340)
    assert yaml.safe_dump(document, sort_keys=True) == before
    assert {key: variant[key] for key in document if key not in {"target", "debuggers", "com_ports", "can_buses"}} == {
        key: document[key] for key in document if key not in {"target", "debuggers", "com_ports", "can_buses"}
    }


def test_the_stage_tells_a_reset_from_the_line_the_rom_prints_on_one() -> None:
    printed = b"ets Jul 29 2019 12:21:46\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\nconfigsip: 0, SPIWP:0xee\r\n"

    assert [match["reason"] for match in ROM_RESET.finditer(printed)] == [b"POWERON_RESET"]
    assert TICK_LINE.search(printed) is None
    assert ROM_RESET.search(b"agentic-hil esp32 image A tick 3\r\n") is None
