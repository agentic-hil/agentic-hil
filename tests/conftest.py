from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["pytester"]

from support import remove_trusted_launcher  # noqa: E402

# Where every test's isolated HOME, config, state and temporary storage go.
# Resolved once, here, out of the system temp root and before any test has
# redirected anything.
#
# It used to be derived from `tmp_path.parent`, which follows `--basetemp`
# wherever it is pointed -- including inside this clone, which is what a
# developer does to keep a scratch path off Windows MAX_PATH. An isolated HOME
# under the working directory is a home inside a project, and the user-level
# half of `agent-install` refuses exactly that through `_external_user_path`, so
# a basetemp choice decided whether unrelated tests passed. The system temp root
# is never inside the repository, whatever the basetemp is.
#
# Per process, so two suites on one machine cannot meet here, and short, because
# every sandbox path is derived from it and MAX_PATH is the reason a basetemp
# gets moved in the first place.
SANDBOX_PREFIX = "ahil-pt-"
SANDBOX_ROOT = Path(tempfile.gettempdir()).resolve() / f"{SANDBOX_PREFIX}{os.getpid()}"
# How long a sibling root may sit untouched before a later session sweeps it. A
# live session creates and removes a sandbox inside its own root on every single
# test, so a root this old is residue: a directory Windows refused to delete
# because a detached child still held a lock file when the test that spawned it
# ended. Under the old location pytest's own numbered root eventually collected
# such leftovers; here nothing else on the machine knows what this directory is.
# Generous, because the cost of being wrong is deleting a live suite's sandbox.
STALE_SANDBOX_AGE_S = 6 * 60 * 60


@pytest.fixture(scope="session", autouse=True)
def _clean_up_trusted_launcher() -> Iterator[None]:
    """Remove the session's trusted launcher, wherever a test created it."""
    yield
    remove_trusted_launcher()


@pytest.fixture(scope="session", autouse=True)
def _clean_up_sandbox_root() -> Iterator[None]:
    """Keep this session's sandbox root to itself, and take nothing with it.

    Each test's own sandbox is removed by its own finalizer; this is what
    catches a test that died before its finalizer ran, and the root directory
    itself."""
    sweep_stale_sandbox_roots()
    yield
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)


def sweep_stale_sandbox_roots(now: float | None = None) -> list[Path]:
    """Remove sandbox roots no session has touched in STALE_SANDBOX_AGE_S.

    Returns what it removed. A root belonging to a suite running beside this one
    is fresh by construction and is never touched, which is the whole property
    that matters here."""
    cutoff = (time.time() if now is None else now) - STALE_SANDBOX_AGE_S
    removed: list[Path] = []
    for candidate in SANDBOX_ROOT.parent.glob(f"{SANDBOX_PREFIX}*"):
        if candidate == SANDBOX_ROOT:
            continue
        with suppress(OSError):
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
                removed.append(candidate)
    return removed

ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_OPENOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_openocd_no_target.py"
FAKE_OPENOCD_MISSING_CFG = ROOT / "tests" / "fixtures" / "fake_openocd_missing_cfg.py"
FAKE_OPENOCD_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_unconfirmed.py"
FAKE_OPENOCD_POST_INIT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_post_init_unconfirmed.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"
FAKE_STLINK_READ_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_read_unconfirmed.py"
FAKE_STLINK_HALT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_halt_unconfirmed.py"
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
    # Owner-only, and outside the repository whatever `--basetemp` says: see
    # SANDBOX_ROOT. Keeping user policy and state out of tmp_path itself still
    # matters, because tests use tmp_path as the workspace cwd.
    SANDBOX_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    test_sandbox = SANDBOX_ROOT / uuid.uuid4().hex[:12]
    home_root = test_sandbox / "home"
    config_root = test_sandbox / "config"
    state_root = test_sandbox / "state"
    temp_root = test_sandbox / "tmp"
    request.addfinalizer(lambda: shutil.rmtree(test_sandbox, ignore_errors=True))
    home_root.mkdir(parents=True)
    temp_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("USERPROFILE", str(home_root))
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home_root / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home_root / ".local" / "share"))
    monkeypatch.setenv("LOCALAPPDATA", str(state_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    # Temporary storage is redirected for the same reason HOME is. `tempfile`
    # answers out of TMPDIR/TEMP/TMP, and the one directory it otherwise names
    # is shared by every suite on the machine: two runs that reached
    # `tools/agent_review_loop.py` in the same second met in one scratch root
    # named after the repository and that second, and deleted each other's round
    # directories. Anything that asks the system for scratch space now lands in
    # this test's sandbox, so two sandboxes cannot meet. `gettempdir` caches its
    # answer, so the module attribute moves too; the variables alone would reach
    # child processes and not this one.
    for variable in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(variable, str(temp_root))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
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


# A real ELF, small enough to write by hand, for the tests that exercise the
# symbol-table fallback. The rest of the suite hands the fakes a four-byte magic
# and nothing more, which is all the artifact validator inspects and all the fake
# GDB needs; a fallback that reads `st_value` and `st_size` out of a section
# table has to be proven against a file that really has one.
ELF_HEADER_SIZES = {32: 52, 64: 64}
ELF_SECTION_HEADER_SIZES = {32: 40, 64: 64}
ELF_SYMBOL_SIZES = {32: 16, 64: 24}
# Section indices in the file this builds: a .text for symbols to be defined
# against, the symbol table, and the strings its names live in.
ELF_TEXT_SECTION = 1
ELF_SYMTAB_SECTION = 2
ELF_STRTAB_SECTION = 3


def elf_with_symbols(entries, *, bits: int = 32, big_endian: bool = False, trailer: bytes = b"") -> bytes:
    """A minimal ELF whose symbol table carries exactly `entries`.

    Each entry is `(name, address, size)` or `(name, address, size, shndx)`; the
    section index defaults to the .text this builds, and passing 0 writes the
    undefined symbol a fallback must not answer from. A name repeated with a
    different address or size is how an ambiguous lookup is expressed.

    `trailer` is appended after the last section, where the fake GDB looks for
    its behaviour marker: bytes past everything the section table describes are
    invisible to a reader that seeks by offset, which is exactly what makes the
    marker and a valid ELF able to share one file.
    """
    order = ">" if big_endian else "<"
    names = bytearray(b"\x00")
    symbols = bytearray(_elf_symbol(order, bits, 0, 0, 0, 0))
    for entry in entries:
        name, address, size = entry[0], entry[1], entry[2]
        section_index = entry[3] if len(entry) > 3 else ELF_TEXT_SECTION
        symbols += _elf_symbol(order, bits, len(names), address, size, section_index)
        names += name.encode("utf-8") + b"\x00"
    header_size = ELF_HEADER_SIZES[bits]
    symbols_offset = header_size
    names_offset = symbols_offset + len(symbols)
    sections_offset = names_offset + len(names)
    sections = b"".join(
        _elf_section(order, bits, section_type, offset, size, link, entry_size)
        for section_type, offset, size, link, entry_size in [
            (0, 0, 0, 0, 0),
            (1, sections_offset, 0, 0, 0),
            (2, symbols_offset, len(symbols), ELF_STRTAB_SECTION, ELF_SYMBOL_SIZES[bits]),
            (3, names_offset, len(names), 0, 0),
        ]
    )
    return _elf_header(order, bits, sections_offset) + bytes(symbols) + bytes(names) + sections + trailer


def _elf_header(order: str, bits: int, sections_offset: int) -> bytes:
    identification = b"\x7fELF" + bytes([1 if bits == 32 else 2, 2 if order == ">" else 1, 1]) + b"\x00" * 9
    address_format = "I" if bits == 32 else "Q"
    return identification + struct.pack(
        f"{order}HHI{address_format}{address_format}{address_format}IHHHHHH",
        2,
        40 if bits == 32 else 183,
        1,
        0,
        0,
        sections_offset,
        0,
        ELF_HEADER_SIZES[bits],
        0,
        0,
        ELF_SECTION_HEADER_SIZES[bits],
        4,
        0,
    )


def _elf_section(order: str, bits: int, section_type: int, offset: int, size: int, link: int, entry_size: int) -> bytes:
    if bits == 32:
        return struct.pack(f"{order}IIIIIIIIII", 0, section_type, 0, 0, offset, size, link, 0, 1, entry_size)
    return struct.pack(f"{order}IIQQQQIIQQ", 0, section_type, 0, 0, offset, size, link, 0, 1, entry_size)


def _elf_symbol(order: str, bits: int, name_offset: int, address: int, size: int, section_index: int) -> bytes:
    if bits == 32:
        return struct.pack(f"{order}IIIBBH", name_offset, address, size, 0x11, 0, section_index)
    return struct.pack(f"{order}IBBHQQ", name_offset, 0x11, 0, section_index, address, size)


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
