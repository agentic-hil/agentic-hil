from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["pytester"]

from support import remove_trusted_launcher  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_up_trusted_launcher() -> Iterator[None]:
    """Remove the session's trusted launcher, wherever a test created it."""
    yield
    remove_trusted_launcher()

ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_OPENOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_openocd_no_target.py"
FAKE_OPENOCD_MISSING_CFG = ROOT / "tests" / "fixtures" / "fake_openocd_missing_cfg.py"
FAKE_OPENOCD_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_unconfirmed.py"
FAKE_OPENOCD_POST_INIT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_post_init_unconfirmed.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"
FAKE_STLINK_PARTIAL_CONFIRMATION = ROOT / "tests" / "fixtures" / "fake_stlink_partial_confirmation.py"
FAKE_STLINK_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_stlink_no_target.py"
FAKE_STLINK_NO_PROBE = ROOT / "tests" / "fixtures" / "fake_stlink_no_probe.py"
FAKE_PYOCD = ROOT / "tests" / "fixtures" / "fake_pyocd.py"
FAKE_PYOCD_UNKNOWN_TARGET = ROOT / "tests" / "fixtures" / "fake_pyocd_unknown_target.py"
FAKE_GDB = ROOT / "tests" / "fixtures" / "fake_gdb.py"



@pytest.fixture(autouse=True)
def isolated_config_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Path:
    # pytest owns tmp_path.parent on every platform, and the trust check accepts
    # it on every platform. It used to divert to Local AppData on Windows because
    # the ACEs a normal profile carries below Temp were read as untrusted; the
    # suite evading the project's own rule was the clearest evidence that the
    # rule was drawn wrong. Keeping user policy and state out of tmp_path itself
    # still matters: tests use tmp_path as the workspace cwd.
    test_sandbox = tmp_path.parent / f"agentic-hil-pytest-{os.getpid()}-{uuid.uuid4().hex}"
    home_root = test_sandbox / "home"
    config_root = test_sandbox / "config"
    state_root = test_sandbox / "state"
    request.addfinalizer(lambda: shutil.rmtree(test_sandbox, ignore_errors=True))
    home_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("USERPROFILE", str(home_root))
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home_root / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home_root / ".local" / "share"))
    monkeypatch.setenv("LOCALAPPDATA", str(state_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    return config_root


@pytest.fixture(autouse=True)
def _no_host_stm32_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap discovery finds no toolchain unless a test hands it one.

    `agentic-hil init` reads the attached bench whatever else is in the workspace,
    so any test that runs it would otherwise spawn whatever STM32CubeProgrammer is
    installed on the machine running the suite and connect to whatever is plugged
    into it — passing on a bare CI container and doing real hardware I/O on a
    developer's bench. A test that wants a toolchain patches this name itself, and
    that patch runs after this one."""
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)


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
    state_root: Path | None = None,
    max_dump_size_bytes: int = 1048576,
    debuggers_yaml: str = "",
    com_ports_yaml: str = "com_ports: {}\n",
    can_buses_yaml: str = "can_buses: {}\n",
    permissions: dict[str, bool] | None = None,
    debugger_name: str = "dut",
    auto_probe_ids: bool = True,
    interface_cfg: str = "interface/stlink.cfg",
    target_cfg: str = "target/stm32f4x.cfg",
    config_path: Path | None = None,
    auto_recover: str | None = None,
    recovery_max_attempts: int | None = None,
    config_version: int | None = None,
    allowed_roots: list[str] | None = None,
    omit_allowed_roots: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    workspace_root = (workspace_root or directory).resolve()
    state_root = (state_root or Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or directory.parent / "user-state") / "agentic-hil").resolve()
    if debugger_executable is None:
        fake_by_type = {"stlink": FAKE_STLINK, "pyocd": FAKE_PYOCD}
        debugger_executable = fake_by_type.get(debugger_type, FAKE_OPENOCD)
    if allow_all_symbols is None:
        allow_all_symbols = allowed_symbols is None
    grants = DEFAULT_TEST_PERMISSIONS if permissions is None else permissions
    primary = {
        "type": debugger_type,
        "executable": debugger_executable.as_posix(),
        "probe_id": probe_id,
        "target_type": target_type,
        "interface": "SWD",
        "interface_cfg": interface_cfg,
        "target_cfg": target_cfg,
        "flash_address": flash_address,
        "timeout_s": 5,
    }
    # Omitted entirely by default, so the common test config exercises the same
    # "policy was never named" path a config written before recovery existed has.
    recovery_lines = "".join(
        [
            "recovery:\n" if auto_recover is not None or recovery_max_attempts is not None else "",
            # Quoted: YAML 1.1 reads a bare `off` as the boolean False.
            f'  auto_recover: "{auto_recover}"\n' if auto_recover is not None else "",
            f"  max_attempts: {recovery_max_attempts}\n" if recovery_max_attempts is not None else "",
        ]
    )
    # Named explicitly by default, exactly as a generated configuration names it.
    # `omit_allowed_roots` is the other file worth testing: one that never named
    # the key and is therefore read under the historical ["build"].
    artifact_roots_line = "" if omit_allowed_roots else f"  allowed_roots: {json.dumps(allowed_roots if allowed_roots is not None else ['build'])}\n"
    config_path = config_path or directory / ".agentic-hil" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # No `version:` by default: the bulk of the suite exercises the file a
    # project already has on disk, which is read under version 1 and still needs
    # a granted read. A test that wants the version 2 model asks for it.
    version_line = "" if config_version is None else f"version: {config_version}\n"
    config_path.write_text(
        f"""{version_line}workspace_root: {workspace_root.as_posix()!r}
state_root: {state_root.as_posix()!r}
target:
  name: "example-target"
  controller: "stm32f4"
debug:
  gdb_executable: {('null' if gdb_executable is None else repr(gdb_executable.as_posix()))}
  allowed_symbols: {(allowed_symbols if allowed_symbols is not None else [])}
  allow_all_symbols: {str(allow_all_symbols).lower()}
  max_dump_size_bytes: {max_dump_size_bytes}
artifacts:
{artifact_roots_line}  allowed_extensions: [".elf", ".hex", ".bin"]
  upload_directory: ".agentic-hil/artifacts"
  max_upload_size_mb: 1
  allow_upload: true
{section_yaml("debuggers", debuggers_yaml, grants, extra={debugger_name: primary}, auto_probe_ids=auto_probe_ids, config_version=config_version)}{section_yaml("com_ports", com_ports_yaml, grants, config_version=config_version)}{section_yaml("can_buses", can_buses_yaml, grants, config_version=config_version)}reports:
  directory: ".agentic-hil/reports"
logs:
  directory: ".agentic-hil/logs"
{recovery_lines}""",
        encoding="utf-8",
    )
    return config_path


# The old flat permission names stay the vocabulary of the test helper: a test
# that wants "no COM write" should say so once, not repeat a permissions block
# under every com_ports entry it happens to declare.
DEFAULT_TEST_PERMISSIONS = {
    "allow_probe": True,
    "allow_flash": True,
    "allow_reset": True,
    "allow_com_read": True,
    "allow_com_write": True,
    "allow_can_read": True,
    "allow_can_write": True,
    "allow_raw_debugger_commands": False,
    "allow_mass_erase": False,
}
READ_PERMISSION_FLAGS = frozenset({"allow_probe", "allow_read"})
SECTION_GRANTS = {
    "debuggers": {"allow_probe": "allow_probe", "allow_flash": "allow_flash", "allow_reset": "allow_reset", "allow_raw_debugger_commands": "allow_raw_debugger_commands", "allow_mass_erase": "allow_mass_erase"},
    "com_ports": {"allow_read": "allow_com_read", "allow_write": "allow_com_write"},
    "can_buses": {"allow_read": "allow_can_read", "allow_write": "allow_can_write"},
}


def section_yaml(section: str, supplied: str, grants: dict[str, bool], extra: dict | None = None, *, auto_probe_ids: bool = True, config_version: int | None = None) -> str:
    """Render one named-device section, giving every entry the permissions it
    does not declare itself.

    Tests hand this helper raw YAML; a round-trip is the only way to reach each
    entry without guessing at indentation, and it also merges the helper's own
    primary debugger with any extra probes a multi-board test adds."""
    document = yaml.safe_load(supplied) or {}
    entries = {**(extra or {}), **(document.get(section) or {})}
    if not entries:
        return f"{section}: {{}}\n"
    defaults = {flag: bool(grants.get(source, False)) for flag, source in SECTION_GRANTS[section].items()}
    if config_version is not None and config_version >= 2:
        # Version 2 has no read permission to grant, and refuses the key.
        defaults = {flag: value for flag, value in defaults.items() if flag not in READ_PERMISSION_FLAGS}
    for entry in entries.values():
        entry.setdefault("permissions", defaults)
    if section == "debuggers" and len(entries) > 1 and auto_probe_ids:
        # A multi-probe config must name each probe's hardware, so give every
        # entry that did not declare one a distinct probe_id. A test that wants
        # a collision sets matching probe_ids itself; a test that wants the
        # missing-probe_id refusal passes auto_probe_ids=False.
        for index, (name, entry) in enumerate(entries.items()):
            if entry.get("probe_id") is None:
                entry["probe_id"] = f"TESTPROBE{index}-{name}"
    return yaml.safe_dump({section: entries}, sort_keys=False, default_flow_style=False)


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
    # Enabled OpenOCD access requires scripts outside the authorized workspace.
    interface_cfg = config_path.parent / "interface.cfg"
    target_cfg = config_path.parent / "target.cfg"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    interface_cfg.write_text("# test interface\n", encoding="utf-8")
    target_cfg.write_text("# test target\n", encoding="utf-8")
    kwargs.setdefault("interface_cfg", interface_cfg.as_posix())
    kwargs.setdefault("target_cfg", target_cfg.as_posix())
    # Extra probes need the same absolute scripts: an OpenOCD entry that keeps
    # the relative schema defaults is rejected at pinning, which would make a
    # multi-board test fail on script paths instead of what it means to check.
    extra = yaml.safe_load(kwargs.get("debuggers_yaml") or "") or {}
    if extra.get("debuggers"):
        for entry in extra["debuggers"].values():
            entry.setdefault("interface_cfg", interface_cfg.as_posix())
            entry.setdefault("target_cfg", target_cfg.as_posix())
        kwargs["debuggers_yaml"] = yaml.safe_dump(extra, sort_keys=False, default_flow_style=False)
    path = write_config(workspace, workspace_root=workspace, config_path=config_path, **kwargs)
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(path.resolve()))
    return path
