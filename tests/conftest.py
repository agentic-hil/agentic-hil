from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"


def write_config(
    directory: Path,
    *,
    debugger_type: str = "openocd",
    debugger_executable: Path | None = None,
    probe_id: str | None = None,
    flash_address: str | None = None,
    com_ports_yaml: str = "com_ports: {}\n",
    can_buses_yaml: str = "can_buses: {}\n",
) -> Path:
    if debugger_executable is None:
        debugger_executable = FAKE_STLINK if debugger_type == "stlink" else FAKE_OPENOCD
    config_path = directory / ".hardci" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""target:
  name: "example-target"
  controller: "stm32f4"
debugger:
  type: "{debugger_type}"
  executable: "{debugger_executable.as_posix()}"
  probe_id: {('null' if probe_id is None else repr(probe_id))}
  interface: "SWD"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  flash_address: {('null' if flash_address is None else repr(flash_address))}
  timeout_s: 5
debug:
  gdb_executable: null
  allowed_symbols: []
  max_dump_size_bytes: 1048576
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
  upload_directory: ".hardci/artifacts"
  max_upload_size_mb: 1
  allow_upload: true
{com_ports_yaml}{can_buses_yaml}reports:
  directory: ".hardci/reports"
logs:
  directory: ".hardci/logs"
""",
        encoding="utf-8",
    )
    return config_path
