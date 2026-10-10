"""An ESP32 flashed, reset and read through the product, on the board's own line.

The ESP32 stage of the bench, collected only where a run names this module, and
run by ``tools/bench_in_container.py --esp32``, which hands the board's USB-UART
bridge in beside the probe and names its node in ``AGENTIC_HIL_BENCH_ESP32``.
There is no debug probe between this host and the chip: esptool reaches the ROM
bootloader through the bridge, and the board's console is the same line. So
every claim below is read off that one line, through the same MCP server that
flashed and reset the chip, and none of it rests on a tool's word about itself.

The images are the tier's own (``firmware/esp32/``): two builds of one program,
A and B, that print their letter and a tick count from boot, ten times a second
by the ROM's delay loop. The letter says which flash the chip runs. The count
says whether the chip was restarted between two reads (it starts again) or not
(it carries on), and no line at all after a reset into the ROM bootloader says
the chip is held there. The rate the count grows at is the ROM's, so nothing
here depends on it: what is compared is which of two counts is larger.

What runs, in order:

* ``debugger_info`` and ``probe_target`` against the board: esptool is found
  and the ROM bootloader names the chip.
* what opening the console does to a running chip, measured for both settings
  of the bridge's control lines (``assert_dtr``/``assert_rts`` both released,
  both asserted) rather than assumed: whether the open restarts the image,
  whether the count carries on across a close and a reopen, and whether a chip
  held in its ROM bootloader stays held through an open. The auto-reset circuit
  on these boards turns the two lines into the chip's EN and IO0, so the answer
  is the board's, and it is recorded.
* for each setting that shows the image at all: image A flashed and seen on the
  console; with the console open, a flash of B, a reset and a probe each refused
  with the session named, and the count carrying on through all three, which is
  what proves they never moved the lines; B flashed after the console is
  stopped, and seen.
* on the setting that leaves a running chip alone: ``reset_target`` run starts
  the image again, halt holds the chip in its ROM bootloader, a probe leaves it
  there, the console stays quiet, and run starts the image once more.

Each line setting has a configuration of its own beside the session's, written
the way an operator declares a board ``init`` does not bind: the bridge's port
by its node, USB ids and serial number (a CH340 publishes none), and one esptool
debugger naming it. Nothing recorded names a node, a serial number, a MAC
address or a path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from agentic_hil.report import overall_success

from .conftest import BENCH_FIRMWARE, BENCH_ONLY, ESP32, ESP32_BRIDGE_IDS, ESP32_ENV, Bench, refuse
from .test_bench_faults import READ_SLICE_S, Server, incident_left_standing, read, received

pytestmark = [pytest.mark.bench, BENCH_ONLY]

RECORDING_SCHEMA = "agentic-hil.esp32-recording/v1"

IMAGES = BENCH_FIRMWARE / "esp32"
MANIFEST = IMAGES / "manifest.json"
# Where the images are put inside the project before a flash names them: under
# `build`, the artifact root every configuration permits.
ESP32_IMAGES_IN_THE_PROJECT = Path("build") / "esp32"

# The two settings of the bridge's control lines, by what `com_session_start`
# does to DTR and RTS when it opens the port.
LINE_SETTINGS = {"released": False, "asserted": True}
# The ROM prints at 115200 baud on a board with a 40 MHz crystal, and the
# images print through the ROM.
CONSOLE_BAUDRATE = 115200

# What each image prints, from `firmware/esp32/banner.c`, and the line the ROM
# prints on every reset, from which a reset seen on the console is told.
TICK_LINE = re.compile(rb"agentic-hil esp32 image (?P<image>[AB]) tick (?P<tick>\d+)\r?\n")
ROM_RESET = re.compile(rb"rst:0x[0-9a-fA-F]+ \((?P<reason>[A-Z0-9_]+)\)")

# How long an image runs before a read that must not be mistaken for a fresh
# boot, and how long the console stays closed between two opens.
BOOT_S = 4.0
CLOSED_S = 2.0
# How long a console has to show no line of an image to count as a chip that is
# not running it: thirty ticks that did not come.
QUIET_S = 3.0
# What a freshly opened console reads before the lines a claim is made from,
# so an open that restarts the chip has finished doing so.
SETTLE_S = 1.0
# The highest count a line read right after a restart can carry.
FRESH_TICKS = 2
# How many lines a claim about the count is made from, and how long they may take.
LINES_WANTED = 5
LINES_TIMEOUT_S = 10.0

# The fields of a tool's answer the evidence keeps: what was done and whether it
# was, never where.
RESULT_EVIDENCE_FIELDS = (
    "ok",
    "error_type",
    "backend",
    "verify",
    "reset_after_flash",
    "held_in_rom_bootloader",
    "mode",
    "target_detected",
    "chip_type",
    "flash_size",
    "target_contacted",
    "retry_safe",
    "quarantined",
    "held_by_com_session",
)


# -- The configuration --------------------------------------------------------


def esp32_variant(
    document: dict,
    *,
    device: str,
    vid: int,
    pid: int,
    serial_number: str | None,
    flash_address: str,
    asserted: bool,
) -> dict:
    """The session's configuration with the ESP32 board as its one debugger and its one port.

    Everything ``init`` wrote stays except what describes the hardware: the
    target is the ESP32, the probe's debugger and port give way to one esptool
    debugger and the bridge's port it names, and there is no CAN bus. The
    debugger names no executable, so the product finds the esptool installed
    beside it, as it would for an operator who left the key out. A bridge that
    publishes no serial number is identified by its USB ids, and the entry says
    so, as version 3 requires.
    """
    variant = copy.deepcopy(document)
    variant["target"] = {"name": "esp32-bench-board", "controller": ESP32}
    variant["debuggers"] = {
        ESP32: {
            "type": "esptool",
            "com_port": ESP32,
            "target_type": ESP32,
            "flash_address": flash_address,
            "permissions": {
                "allow_flash": True,
                "allow_reset": True,
                "allow_debug_execution": False,
                "allow_raw_debugger_commands": False,
                "allow_mass_erase": False,
            },
        }
    }
    port: dict[str, object] = {"device": device, "vid": vid, "pid": pid}
    if serial_number:
        port["serial_number"] = serial_number
    else:
        port["identity_source"] = "vid_pid"
    port.update(
        {
            "baudrate": CONSOLE_BAUDRATE,
            "assert_dtr": asserted,
            "assert_rts": asserted,
            "permissions": {"allow_write": False},
        }
    )
    variant["com_ports"] = {ESP32: port}
    variant["can_buses"] = {}
    return variant


@dataclass(frozen=True)
class Esp32Images:
    """The two images inside the project, and what their manifest says about them."""

    manifest: dict
    paths: dict[str, str]
    digests: dict[str, str]

    @property
    def flash_address(self) -> str:
        return str(self.manifest["flash_address"])

    def provenance(self) -> dict:
        return {
            "chip": self.manifest.get("chip"),
            "flash_address": self.flash_address,
            "built_with_esptool": self.manifest.get("esptool"),
            "toolchain": {key: self.manifest.get("toolchain", {}).get(key) for key in ("release", "gcc")},
            "images": {letter: self.digests[letter] for letter in sorted(self.digests)},
        }


@pytest.fixture(scope="module")
def esp32_images(configured_bench: Bench) -> Esp32Images:
    """Both images copied into the project and checked against their manifest before anything is flashed."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    placed = configured_bench.project / ESP32_IMAGES_IN_THE_PROJECT
    placed.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    digests: dict[str, str] = {}
    for name, entry in manifest["images"].items():
        target = placed / name
        shutil.copyfile(IMAGES / name, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            refuse(f"the ESP32 image {name} hashes to {digest}, and its manifest says {entry['sha256']}")
        paths[entry["image"]] = (ESP32_IMAGES_IN_THE_PROJECT / name).as_posix()
        digests[entry["image"]] = digest
    if sorted(paths) != ["A", "B"]:
        refuse(f"the ESP32 image manifest names images {sorted(paths)}, and this stage flashes A and B")
    return Esp32Images(manifest=manifest, paths=paths, digests=digests)


@pytest.fixture(scope="module")
def esp32_benches(configured_bench: Bench, esp32_images: Esp32Images) -> dict[str, Bench]:
    """The configured bench once per line setting, with the ESP32 board declared and driving it.

    Fails rather than skips like ``usb_uart_bench``: a run that was handed no
    board, a bridge the product's inventory does not show at the node it was
    handed in at or shows under other USB ids, and a ``doctor`` that is red.
    """
    node = os.environ.get(ESP32_ENV)
    if not node:
        refuse(f"this run was handed no ESP32 board ({ESP32_ENV} is not set), and the ESP32 stage drives one")
    code, inventory = configured_bench.document("com-ports")
    found = [port for port in inventory.get("ports") or [] if isinstance(port, dict) and port.get("device") == node]
    if code != 0 or len(found) != 1:
        # By node and USB ids alone: the rest of an entry carries serial numbers.
        shown = [{key: listed.get(key) for key in ("device", "vid", "pid")} for listed in inventory.get("ports") or [] if isinstance(listed, dict)]
        said = inventory.get("summary") if code != 0 else json.dumps(shown)
        refuse(f"the product's inventory shows {len(found)} port(s) at {node}, where the ESP32 board's bridge was handed in: {said}")
    port = found[0]
    vid, pid = port.get("vid"), port.get("pid")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (vid, pid)) or (vid, pid) not in ESP32_BRIDGE_IDS:
        refuse(f"the product's inventory shows USB ids {vid!r}:{pid!r} at {node}, which are not the bridge of an ESP32 board")
    document = configured_bench.configuration()
    benches: dict[str, Bench] = {}
    for setting, asserted in LINE_SETTINGS.items():
        variant = esp32_variant(
            document,
            device=port.get("stable_device") or node,
            vid=vid,
            pid=pid,
            serial_number=port.get("serial_number") or None,
            flash_address=esp32_images.flash_address,
            asserted=asserted,
        )
        written = configured_bench.config_root / f"{ESP32}-{setting}" / configured_bench.config.name
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(yaml.safe_dump(variant, sort_keys=False), encoding="utf-8")
        declared = replace(configured_bench, config=written, serial_port=ESP32)
        verdict = declared.run("doctor")
        if verdict.returncode != 0:
            refuse(f"the bench with the ESP32 board declared ({setting} control lines) is not bound to hardware:\n{verdict.stdout}")
        benches[setting] = declared
    return benches


# -- The server, the run and the console --------------------------------------


@contextmanager
def served(bench: Bench, stderr_path: Path) -> Iterator[Server]:
    """One greeted MCP server for one bench; the session and the run it may hold ended afterwards.

    A test that left the bench blocked fails for it, after the bench is cleared,
    unless it is already failing for something else.
    """
    server = Server(bench, stderr_path)
    finished = False
    try:
        server.greet()
        yield server
        finished = True
    finally:
        try:
            server.try_call("com_session_stop", {"port_id": ESP32})
            server.try_call("bench_run_stop")
        finally:
            server.close()
            left = incident_left_standing(bench)
        if left is not None and finished:
            pytest.fail(left, pytrace=False)


def evidence_of(result: dict) -> dict:
    """What a tool's answer says was done, without anything that says where."""
    kept = {key: result[key] for key in RESULT_EVIDENCE_FIELDS if key in result}
    artifact = result.get("artifact")
    if isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str):
        kept["artifact_sha256"] = artifact["sha256"]
    return kept


def require_success(result: dict, action: str) -> None:
    if not overall_success(result):
        pytest.fail(f"Agentic HIL {action} failed: {json.dumps(evidence_of(result), sort_keys=True)}: {result.get('summary')}", pytrace=False)


def start_run(server: Server, label: str) -> None:
    """Declare the board's debugger and its port, which are one line, for the whole of a sequence."""
    _, started = server.call(
        "bench_run_start",
        {"devices": [{"kind": "debugger", "id": ESP32}, {"kind": "uart", "id": ESP32}], "label": label},
    )
    require_success(started, "bench_run_start")


def stop_run(server: Server) -> None:
    _, stopped = server.call("bench_run_stop")
    require_success(stopped, "bench_run_stop")


def flash(server: Server, images: Esp32Images, letter: str) -> dict:
    """One image flashed and started; the answer, asserted to say exactly that image was written and verified."""
    _, flashed = server.call("flash_firmware", {"image_path": images.paths[letter], "reset_after_flash": True})
    require_success(flashed, f"flash of image {letter}")
    assert flashed.get("backend") == "esptool", evidence_of(flashed)
    assert flashed.get("verify") is True, evidence_of(flashed)
    assert flashed.get("held_in_rom_bootloader") is False, evidence_of(flashed)
    assert evidence_of(flashed).get("artifact_sha256") == images.digests[letter], evidence_of(flashed)
    return flashed


def reset(server: Server, mode: str) -> dict:
    _, answered = server.call("reset_target", {"mode": mode})
    require_success(answered, f"reset_target {mode}")
    assert answered.get("held_in_rom_bootloader") is (mode == "halt"), evidence_of(answered)
    return answered


class Console:
    """One COM session on the board's line, and every byte it handed over, kept whole.

    Kept whole because a read ends wherever the bytes ran out, so a line is
    often split across two of them; every question below is asked of the whole
    stream, from a position a caller marked at a line boundary.
    """

    def __init__(self, server: Server) -> None:
        self.server = server
        self.data = b""
        self.open = False

    def start(self) -> Console:
        # Not cleared on open: a reset the open itself causes prints the ROM's
        # line at once, and clearing the buffer could take it away.
        errored, opened = self.server.call("com_session_start", {"port_id": ESP32, "clear_buffer": False})
        assert errored is False and opened.get("ok") is True, opened
        self.open = True
        return self

    def stop(self) -> None:
        errored, stopped = self.server.call("com_session_stop", {"port_id": ESP32})
        assert errored is False and stopped.get("ok") is True, stopped
        self.open = False

    def take(self, wait_timeout_s: float) -> None:
        self.data += received(read(self.server, ESP32, wait_timeout_s))

    def mark(self) -> int:
        """Where the next whole line starts: just past the last line break read so far."""
        return self.data.rfind(b"\n") + 1

    def ticks(self, since: int = 0) -> list[tuple[str, int]]:
        return [(match["image"].decode("ascii"), int(match["tick"])) for match in TICK_LINE.finditer(self.data, since)]

    def rom_resets(self, since: int = 0) -> list[str]:
        return [match["reason"].decode("ascii") for match in ROM_RESET.finditer(self.data, since)]

    def read_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            self.take(min(READ_SLICE_S, remaining))

    def read_lines(self, count: int, since: int, timeout_s: float = LINES_TIMEOUT_S) -> list[tuple[str, int]]:
        """At least `count` image lines from `since`, or as many as came within the timeout."""
        deadline = time.monotonic() + timeout_s
        while len(self.ticks(since)) < count and time.monotonic() < deadline:
            self.take(READ_SLICE_S)
        return self.ticks(since)

    def summary(self, since: int = 0) -> dict:
        ticks = self.ticks(since)
        return {
            "bytes": len(self.data) - since,
            "images": sorted({image for image, _ in ticks}),
            "lines": len(ticks),
            "first_tick": ticks[0][1] if ticks else None,
            "last_tick": ticks[-1][1] if ticks else None,
            "rom_resets": self.rom_resets(since),
        }


def watch(server: Server, *, lines: int | None = None, seconds: float | None = None) -> Console:
    """Open the console, read `lines` image lines or for `seconds`, and stop it again."""
    console = Console(server).start()
    try:
        if lines is not None:
            console.read_lines(lines, 0)
        if seconds is not None:
            console.read_for(seconds)
    finally:
        console.stop()
    return console


def counts(ticks: list[tuple[str, int]]) -> list[int]:
    return [tick for _, tick in ticks]


def consecutive(ticks: list[tuple[str, int]]) -> bool:
    numbers = counts(ticks)
    return bool(numbers) and numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def record(record_property, name: str, evidence: dict) -> None:
    record_property(name, json.dumps({"schema": RECORDING_SCHEMA, **evidence}, sort_keys=True, separators=(",", ":")))


# -- What opening the console does --------------------------------------------


def measure(bench: Bench, images: Esp32Images, stderr_path: Path) -> dict:
    """What one line setting's console open does to a running chip and to a held one."""
    with served(bench, stderr_path) as server:
        start_run(server, "esp32-console-open-measurement")
        flash(server, images, "A")
        time.sleep(BOOT_S)
        first = watch(server, lines=LINES_WANTED)
        time.sleep(CLOSED_S)
        again = watch(server, lines=LINES_WANTED)
        reset(server, "halt")
        held = watch(server, seconds=QUIET_S)
        reset(server, "run")
        stop_run(server)
    first_ticks, again_ticks = counts(first.ticks()), counts(again.ticks())
    shows_image = bool(first_ticks)
    restarted = bool(first.rom_resets()) or (shows_image and first_ticks[0] <= FRESH_TICKS)
    counted_on = bool(first_ticks and again_ticks) and again_ticks[0] > first_ticks[-1] and not again.rom_resets()
    held_through = not held.ticks() and not held.rom_resets()
    return {
        "first_open": first.summary(),
        "reopen_after_close": again.summary(),
        "open_after_reset_halt": held.summary(),
        "shows_image_on_open": shows_image,
        "restarted_on_open": restarted,
        "counted_on_across_close_and_reopen": counted_on,
        "held_through_open": held_through,
        "keeps_a_running_chip_running": shows_image and not restarted and counted_on and held_through,
    }


@pytest.fixture(scope="module")
def line_behaviour(esp32_benches: dict[str, Bench], esp32_images: Esp32Images, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    """Both line settings measured once, each through a server and a run of its own."""
    logs = tmp_path_factory.mktemp("esp32-line-measurement")
    return {setting: measure(esp32_benches[setting], esp32_images, logs / f"{setting}.stderr") for setting in LINE_SETTINGS}


def running_setting(line_behaviour: dict[str, dict]) -> str:
    """The setting whose open leaves a running chip alone: asserted where both do, as the product's default."""
    for setting in ("asserted", "released"):
        if line_behaviour[setting]["keeps_a_running_chip_running"]:
            return setting
    pytest.fail(f"neither control-line setting opened the console without disturbing the chip: {json.dumps(line_behaviour, sort_keys=True)}", pytrace=False)


# -- The tests ----------------------------------------------------------------


def test_esptool_is_found_and_the_rom_bootloader_names_the_chip(esp32_benches: dict[str, Bench], esp32_images: Esp32Images, tmp_path: Path, record_property) -> None:
    source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT") or None
    run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID") or None
    if source_commit is not None:
        assert re.fullmatch(r"[0-9a-f]{40}", source_commit), source_commit
    if run_id is not None:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", run_id), run_id
    with served(esp32_benches["asserted"], tmp_path / "mcp-stdio.stderr") as server:
        _, info = server.call("debugger_info")
        require_success(info, "debugger_info")
        _, probed = server.call("probe_target")
        record(
            record_property,
            "esp32_probe_v1",
            {
                "source_commit": source_commit,
                "run_id": run_id,
                "esptool_version": info.get("version"),
                "chip": info.get("chip"),
                "probe": evidence_of(probed),
                **esp32_images.provenance(),
            },
        )
        require_success(probed, "probe_target")
    assert info.get("backend") == "esptool", info
    assert str(info.get("version") or "").startswith("5."), info
    assert probed.get("target_detected") is True, evidence_of(probed)
    assert "ESP32" in str(probed.get("chip_type") or ""), evidence_of(probed)
    assert probed.get("flash_size"), evidence_of(probed)
    assert probed.get("held_in_rom_bootloader") is False, evidence_of(probed)


def test_what_opening_the_console_does_to_the_chip_is_measured_for_both_control_line_settings(line_behaviour: dict[str, dict], record_property) -> None:
    """Recorded, not assumed: which setting restarts the chip on open is the board's auto-reset circuit's answer."""
    record(record_property, "esp32_console_open_v1", {"settings": {setting: {"assert_dtr": asserted, "assert_rts": asserted, **line_behaviour[setting]} for setting, asserted in LINE_SETTINGS.items()}})
    shown = [setting for setting in LINE_SETTINGS if line_behaviour[setting]["shows_image_on_open"]]
    assert shown, f"the console showed no line of the flashed image with either control-line setting: {json.dumps(line_behaviour, sort_keys=True)}"
    for setting in shown:
        assert line_behaviour[setting]["first_open"]["images"] == ["A"], line_behaviour[setting]


def flash_seen_and_refused(bench: Bench, images: Esp32Images, stderr_path: Path, evidence: dict) -> None:
    """Image A on the console, a flash, a reset and a probe refused while it is open, and B once it is closed.

    `evidence` is filled as the sequence goes, so a failure still leaves what
    was seen up to it.
    """
    with served(bench, stderr_path) as server:
        start_run(server, "esp32-flash-and-console")
        evidence["flash_a"] = evidence_of(flash(server, images, "A"))

        console = Console(server).start()
        console.read_for(SETTLE_S)
        since = console.mark()
        before = console.read_lines(LINES_WANTED, since)
        evidence["console_after_flash_a"] = console.summary(since)
        assert len(before) >= LINES_WANTED and {image for image, _ in before} == {"A"} and consecutive(before), evidence
        refusals: dict[str, dict] = {}
        evidence["refused_while_the_console_was_open"] = refusals
        for tool, arguments in (
            ("flash_firmware", {"image_path": images.paths["B"], "reset_after_flash": True}),
            ("reset_target", {"mode": "run"}),
            ("probe_target", {}),
        ):
            errored, refused = server.call(tool, arguments)
            refusals[tool] = evidence_of(refused)
            assert errored is True, evidence
            assert refused.get("error_type") == "device_busy", evidence
            assert refused.get("held_by_com_session") == ESP32, evidence
        through = console.read_lines(len(before) + LINES_WANTED, since)
        evidence["console_through_the_refusals"] = console.summary(since)
        console.stop()
        # The count carrying on, line after line, is what proves no refused call
        # moved the lines: a restart starts it again, and a reset into the ROM
        # bootloader stops it.
        assert len(through) >= len(before) + LINES_WANTED, evidence
        assert {image for image, _ in through} == {"A"}, evidence
        assert consecutive(through), evidence
        assert not console.rom_resets(since), evidence

        evidence["flash_b"] = evidence_of(flash(server, images, "B"))
        console = Console(server).start()
        console.read_for(SETTLE_S)
        since = console.mark()
        after = console.read_lines(LINES_WANTED, since)
        evidence["console_after_flash_b"] = console.summary(since)
        console.stop()
        stop_run(server)
    assert len(after) >= LINES_WANTED and {image for image, _ in after} == {"B"} and consecutive(after), evidence


def test_a_flash_is_seen_on_the_console_refused_while_the_console_holds_the_line_and_seen_after_it_closes(
    esp32_benches: dict[str, Bench], esp32_images: Esp32Images, line_behaviour: dict[str, dict], tmp_path: Path, record_property
) -> None:
    """Once for each control-line setting whose console shows the running image.

    One test rather than one per setting: a setting measured silent on this
    board would have to be skipped, and a skip is no verdict on a bench.
    """
    shown = [setting for setting in LINE_SETTINGS if line_behaviour[setting]["shows_image_on_open"]]
    if not shown:
        pytest.fail(f"the console showed no line of the flashed image with either control-line setting: {json.dumps(line_behaviour, sort_keys=True)}", pytrace=False)
    evidence: dict[str, dict] = {}
    try:
        for setting in shown:
            evidence[setting] = {}
            flash_seen_and_refused(esp32_benches[setting], esp32_images, tmp_path / f"mcp-stdio-{setting}.stderr", evidence[setting])
    finally:
        record(record_property, "esp32_flash_console_v1", {"settings": evidence})


def test_reset_run_starts_the_image_again_and_reset_halt_holds_the_chip_in_its_rom_bootloader(
    esp32_benches: dict[str, Bench], esp32_images: Esp32Images, line_behaviour: dict[str, dict], tmp_path: Path, record_property
) -> None:
    setting = running_setting(line_behaviour)
    evidence: dict = {"setting": setting}
    try:
        with served(esp32_benches[setting], tmp_path / "mcp-stdio.stderr") as server:
            start_run(server, "esp32-reset-modes")
            evidence["flash_a"] = evidence_of(flash(server, esp32_images, "A"))
            time.sleep(BOOT_S)
            running = watch(server, lines=LINES_WANTED)
            evidence["running"] = running.summary()

            evidence["reset_run"] = evidence_of(reset(server, "run"))
            restarted = watch(server, lines=LINES_WANTED)
            evidence["after_reset_run"] = restarted.summary()

            evidence["reset_halt"] = evidence_of(reset(server, "halt"))
            _, probed = server.call("probe_target")
            evidence["probe_while_held"] = evidence_of(probed)
            require_success(probed, "probe_target while held")
            held = watch(server, seconds=QUIET_S)
            evidence["while_held"] = held.summary()

            evidence["reset_run_again"] = evidence_of(reset(server, "run"))
            released = watch(server, lines=LINES_WANTED)
            evidence["after_reset_run_again"] = released.summary()
            stop_run(server)
    finally:
        record(record_property, "esp32_reset_modes_v1", evidence)

    before = counts(running.ticks())
    assert len(before) >= LINES_WANTED and not running.rom_resets(), evidence
    # Started again: the first count after the reset is below the last before it.
    after = counts(restarted.ticks())
    assert after and after[0] < before[-1], evidence
    assert probed.get("held_in_rom_bootloader") is True and probed.get("target_detected") is True, evidence
    assert not held.ticks() and not held.rom_resets(), evidence
    assert released.ticks() and {image for image, _ in released.ticks()} == {"A"}, evidence
