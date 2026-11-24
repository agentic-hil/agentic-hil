"""Host acceptance checks for the checkout-only Pico H example."""

from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath

import yaml

from agentic_hil.config import load_config
from agentic_hil.test_reactor import TestStep as PlanStep
from agentic_hil.test_reactor import load_test_config

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "pico-rp2040_demo"
DEMO_PLAN = DEMO / "testconfig.yaml"
BENCH_PLAN = ROOT / "tests" / "hil" / "pico_rp2040_shared_bench.yaml"
ELF = PurePosixPath("build/Debug/pico-rp2040_demo.elf")
BENCH_ELF = (PurePosixPath(DEMO.relative_to(ROOT).as_posix()) / ELF).as_posix()
BANNER = "Hello World"
BENCH_PATTERN = r"(?m)^Hello World\r?\n"


def test_demo_plan() -> None:
    plan = load_test_config(str(DEMO_PLAN), str(DEMO))
    raw = yaml.safe_load(DEMO_PLAN.read_text(encoding="utf-8"))
    expected = [
        PlanStep("flash", {"image_path": ELF.as_posix(), "reset_after_flash": False}, device="dut"),
        PlanStep("uart_open", {"clear_buffer": True}, device="dut_uart"),
        PlanStep("reset", {"mode": "run"}, device="dut"),
        PlanStep("uart_read", {"comparator": {"equals": BANNER}, "timeout_s": 5}, device="dut_uart"),
    ]
    if raw["version"] != 3 or plan.steps != expected:
        raise AssertionError("The demo plan does not implement the Pico flash, open, reset and read loop.")


def test_shared_bench_plan() -> None:
    plan = load_test_config(str(BENCH_PLAN), str(ROOT))
    raw = yaml.safe_load(BENCH_PLAN.read_text(encoding="utf-8"))
    assert raw.keys() == {"version", "name", "steps"}
    assert raw["version"] == 3
    assert plan.steps == [
        PlanStep("flash", {"image_path": BENCH_ELF, "reset_after_flash": False}, debugger="pico_h"),
        PlanStep("uart_open", {"clear_buffer": True}, port_id="pico_h_uart"),
        PlanStep("reset", {"mode": "run"}, debugger="pico_h"),
        PlanStep("uart_expect", {"pattern": BENCH_PATTERN, "timeout_s": 5}, port_id="pico_h_uart"),
    ]


def test_plans_expect_the_firmware_line() -> None:
    source = (DEMO / "Src" / "main.c").read_text(encoding="utf-8")
    demo = load_test_config(str(DEMO_PLAN), str(DEMO))
    bench = load_test_config(str(BENCH_PLAN), str(ROOT))
    comparator = demo.steps[-1].arguments.get("comparator")
    pattern = bench.steps[-1].arguments.get("pattern")

    # Keep string tokens intact while removing comments that can quote an example call.
    code = re.sub(
        r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|//[^\n]*|/\*.*?\*/''',
        lambda match: match[1] or " ",
        source,
        flags=re.DOTALL,
    )
    output_calls = re.findall(
        r'\b(puts|printf|uart\w*(?:put[s]?|write|print)\w*|write_uart\w*)\s*\(\s*("(?:\\.|[^"\\])*")',
        code,
    )
    lines = {ast.literal_eval(literal) + ("\n" if function == "puts" else "") for function, literal in output_calls}
    if not (
        f"{BANNER}\n" in lines
        and isinstance(comparator, dict)
        and comparator.get("equals") == BANNER
        and isinstance(pattern, str)
        and re.fullmatch(pattern, f"{BANNER}\n")
    ):
        raise AssertionError("The firmware does not print the terminated line expected by both plans.")


def test_authoritative_debug_probe_configuration(tmp_path: Path) -> None:
    document = yaml.safe_load((DEMO / "config.example.yaml").read_text(encoding="utf-8"))
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({**document, "workspace_root": str(workspace), "state_root": str(state)}),
        encoding="utf-8",
    )
    config = load_config(str(config_file), str(workspace))
    debugger = config.debuggers.get("dut")
    uart = config.com_ports.get("dut_uart")
    if debugger is None or uart is None:
        raise AssertionError("The loaded Pico configuration does not bind the Debug Probe and its read-only UART as required.")

    permissions = debugger.permissions
    roots = [PurePosixPath(root.replace("\\", "/")) for root in config.artifacts.allowed_roots]
    if not (
        config.config_version == 3
        and debugger.type == "pyocd"
        and debugger.target_type == "rp2040"
        and permissions.allow_flash
        and permissions.allow_reset
        and permissions.allow_debug_execution
        and not permissions.allow_raw_debugger_commands
        and not permissions.allow_mass_erase
        and uart.baudrate == 115200
        and not uart.permissions.allow_write
        and debugger.probe_id == uart.serial_number
        and _host_placeholder(debugger.probe_id)
        and _host_placeholder(uart.device)
        and _host_placeholder(document["workspace_root"])
        and _host_placeholder(document["state_root"])
        and (debugger.executable is None or debugger.executable == "pyocd" or _host_placeholder(debugger.executable))
        and ELF.suffix in config.artifacts.allowed_extensions
        and any(ELF.is_relative_to(root) for root in roots)
    ):
        raise AssertionError("The loaded Pico configuration does not bind the Debug Probe and its read-only UART as required.")


def _host_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.search(r"<[^<>]+>|\{[^{}]+\}|\b(?:example|placeholder|replace|your|path/to)\b", value, re.IGNORECASE)
    )


def test_repository_demo_delivery() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    support = (ROOT / "docs" / "support.md").read_text(encoding="utf-8")
    readme = (DEMO / "README.md").read_text(encoding="utf-8").lower()

    releases = re.findall(
        r"^##[ \t]+(?:\[Unreleased\]|\[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2})[ \t]*\n(.*?)(?=^##[ \t]|\Z)",
        changelog, re.M | re.S,
    )
    demo_entry = any(
        re.search(r"pico-rp2040_demo|(?=.*\b(?:Pico H|RP2040)\b).*\bdemo\b", entry, re.I | re.S)
        for release in releases
        for entry in re.findall(r"(?m)^[-*+][ \t]+[^\n]*(?:\n[ \t]+[^\n]*)*", _section(release, "###", "Added"))
    )
    rp2040 = _section(configuration, "##", "An RP2040 behind a Raspberry Pi Debug Probe")
    readme_text = " ".join(readme.split())
    support_text = " ".join(support.lower().split())
    support_paragraphs = [" ".join(paragraph.split()) for paragraph in re.split(r"\n\s*\n", support.lower())]
    qualification = (
        "cmsis-dap", "pyocd", "0.28.0", "pico h", "rp2040", "raspberry pi debug probe",
        "flash", "reset", "uart",
    )
    readme_details = (
        "cmake", "ninja", "arm-none-eabi", "pyocd", "swd", "debug connector",
        "gp0", "gp1", "gnd", "usb", "--preset debug", "cmake --build", "config.example.yaml",
        "workspace_root", "state_root", "agentic-hil debugger-probes", "debuggers.dut.probe_id",
        "com_ports.dut_uart.serial_number", "agentic-hil test-reactor --test-config testconfig.yaml",
    )
    expected_manifest_lines = {"prune examples/pico-rp2040_demo", "exclude tests/test_pico_rp2040_demo.py"}
    if not (
        demo_entry
        and expected_manifest_lines <= {" ".join(line.split()) for line in manifest.splitlines()}
        and all(detail in readme_text for detail in readme_details)
        and _has_debug_probe_entries(rp2040)
        and any(
            all(detail in paragraph for detail in qualification)
            and re.search(r"\b(?:recorded|proven|confirmed|passed|verified|demonstrated)\b", paragraph)
            for paragraph in support_paragraphs
        )
        and re.search(r"\[[^\]]+\]\([^)]*pico-rp2040_demo[^)]*\)", support)
        and re.search(r"nucleo-f446re.{0,100}\breference\b|\breference\b.{0,100}nucleo-f446re", support_text)
        and re.search(r"\|\s*probe\s*\|\s*backend\s*\|", support_text)
        and re.search(r"\bno (?:list|table) of (?:supported or unsupported )?boards\b", support_text)
    ):
        raise AssertionError("The repository does not yet document and package the Pico demo as required.")


def _section(markdown: str, level: str, heading: str) -> str:
    match = re.search(rf"^{level}\s+{re.escape(heading)}[ \t]*$", markdown, re.MULTILINE)
    if match is None:
        return ""
    return re.split(rf"^#{{1,{len(level)}}}[ \t]+", markdown[match.end():], maxsplit=1, flags=re.MULTILINE)[0]


def _has_debug_probe_entries(section: str) -> bool:
    debug = {}
    ports = {}
    for block in re.findall(r"^(?:`{3}|~{3})ya?ml[ \t]*\n(.*?)^(?:`{3}|~{3})[ \t]*$", section, re.MULTILINE | re.DOTALL):
        fragment = yaml.safe_load(block)
        if isinstance(fragment, dict):
            debug.update(fragment.get("debuggers", {}))
            ports.update(fragment.get("com_ports", {}))
    debugger = debug.get("dut", {})
    uart = ports.get("dut_uart", {})
    permissions = debugger.get("permissions", {})
    return (
        debugger.get("type") == "pyocd"
        and debugger.get("target_type") == "rp2040"
        and permissions.get("allow_flash") is True
        and permissions.get("allow_reset") is True
        and permissions.get("allow_debug_execution") is True
        and permissions.get("allow_raw_debugger_commands") is False
        and permissions.get("allow_mass_erase") is False
        and _host_placeholder(debugger.get("probe_id"))
        and uart.get("serial_number") == debugger.get("probe_id")
        and uart.get("baudrate") == 115200
        and _host_placeholder(uart.get("device"))
    )
