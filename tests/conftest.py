from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"
FAKE_PYOCD = ROOT / "tests" / "fixtures" / "fake_pyocd.py"
FAKE_GDB = ROOT / "tests" / "fixtures" / "fake_gdb.py"
SIM_NTC_ADAPTER = ROOT / "examples" / "adapters" / "sim_ntc_adapter.py"


@pytest.fixture(autouse=True)
def isolated_config_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_root = tmp_path / "user-config"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    return config_root


def write_config(
    directory: Path,
    *,
    debugger_type: str = "openocd",
    debugger_executable: Path | None = None,
    probe_id: str | None = None,
    target_type: str | None = None,
    flash_address: str | None = None,
    gdb_executable: Path | None = None,
    allowed_symbols: list[str] | None = None,
    allow_all_symbols: bool | None = None,
    workspace_root: Path | None = None,
    max_dump_size_bytes: int = 1048576,
    devices_yaml: str = "devices: {}\n",
    com_ports_yaml: str = "com_ports: {}\n",
    can_buses_yaml: str = "can_buses: {}\n",
    adapters_yaml: str = "adapters: {}\n",
    permissions_yaml: str | None = None,
    config_path: Path | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    workspace_root = (workspace_root or directory).resolve()
    if debugger_executable is None:
        fake_by_type = {"stlink": FAKE_STLINK, "pyocd": FAKE_PYOCD}
        debugger_executable = fake_by_type.get(debugger_type, FAKE_OPENOCD)
    if allow_all_symbols is None:
        allow_all_symbols = allowed_symbols is None
    if permissions_yaml is None:
        permissions_yaml = """permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_com_read: true
  allow_com_write: true
  allow_can_read: true
  allow_can_write: true
  allow_adapter_read: true
  allow_adapter_write: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false
"""
    config_path = config_path or directory / ".agentic-hil" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""workspace_root: {workspace_root.as_posix()!r}
target:
  name: "example-target"
  controller: "stm32f4"
{devices_yaml}debugger:
  type: "{debugger_type}"
  executable: "{debugger_executable.as_posix()}"
  probe_id: {('null' if probe_id is None else repr(probe_id))}
  target_type: {('null' if target_type is None else repr(target_type))}
  interface: "SWD"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  flash_address: {('null' if flash_address is None else repr(flash_address))}
  timeout_s: 5
debug:
  gdb_executable: {('null' if gdb_executable is None else repr(gdb_executable.as_posix()))}
  allowed_symbols: {(allowed_symbols if allowed_symbols is not None else [])}
  allow_all_symbols: {str(allow_all_symbols).lower()}
  max_dump_size_bytes: {max_dump_size_bytes}
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
  upload_directory: ".agentic-hil/artifacts"
  max_upload_size_mb: 1
  allow_upload: true
{com_ports_yaml}{can_buses_yaml}{adapters_yaml}{permissions_yaml}reports:
  directory: ".agentic-hil/reports"
logs:
  directory: ".agentic-hil/logs"
""",
        encoding="utf-8",
    )
    return config_path


def write_authoritative_config(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_root: Path | None = None,
    **kwargs,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    root = (config_root or workspace.parent / "user-config").resolve()
    monkeypatch.setenv("APPDATA", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    from agentic_hil.config import project_config_path

    config_path = project_config_path(workspace)
    path = write_config(workspace, workspace_root=workspace, config_path=config_path, **kwargs)

    # Enabled OpenOCD access requires scripts outside the authorized workspace.
    interface_cfg = config_path.parent / "interface.cfg"
    target_cfg = config_path.parent / "target.cfg"
    interface_cfg.write_text("# test interface\n", encoding="utf-8")
    target_cfg.write_text("# test target\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('interface_cfg: "interface/stlink.cfg"', f'interface_cfg: "{interface_cfg.as_posix()}"').replace(
            'target_cfg: "target/stm32f4x.cfg"', f'target_cfg: "{target_cfg.as_posix()}"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(path.resolve()))
    return path
