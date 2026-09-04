from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["pytester"]

from support import remove_trusted_launcher, sweep_stale_launchers  # noqa: E402

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
# gets moved in the first place. On POSIX the parent is /tmp itself rather than
# gettempdir(): macOS answers gettempdir() with its /var/folders/... token path,
# which is long enough that a per-test TMPDIR derived from it pushes the CAN
# broker's AF_UNIX socket addresses past the platform's 104-byte limit, and the
# whole macOS CI matrix failed on exactly that. /tmp is the shortest root every
# POSIX platform has; resolved, because on macOS /tmp is itself a symlink to
# /private/tmp and the path rules refuse a component that is a symlink, so the
# sandbox has to carry the real spelling. The per-process directory under it is
# this user's own, and everything below stays per-test as before.
SANDBOX_PREFIX = "ahil-pt-"
_SANDBOX_PARENT = Path("/tmp").resolve() if os.name == "posix" else Path(tempfile.gettempdir()).resolve()
SANDBOX_ROOT = _SANDBOX_PARENT / f"{SANDBOX_PREFIX}{os.getpid()}"
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
    """Remove the session's trusted launcher, wherever a test created it.

    The sweep on the way in is what catches the sessions that never reached the
    finalizer on the way out: a run that is killed or crashes leaves its
    launcher in the real home under its own pid, and nothing else on the machine
    knows what that directory is. It only ever removes a sibling whose pid names
    no live process, so a suite running beside this one keeps its own."""
    sweep_stale_launchers()
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


class SandboxEscaped(AssertionError):
    """A test resolved a user-level path back onto the operator's own profile."""


def user_level_paths() -> tuple[Path, ...]:
    """Where this process would put user-level files if it wrote one right now.

    `Path.home()` is where all three agent integrations live: `~/.claude.json`
    and `~/.claude/skills`, `~/.codex`, `~/.config/opencode`. `tool_owned_user_roots`
    names every tree Agentic HIL creates for itself, the authoritative
    configurations, the state root and `~/.agentic-hil`, both where the
    environment points now and where a profile with the variable unset falls
    back to. Each of them is read out of the variables `isolated_config_environment`
    redirects, and neither call opens or creates anything, which is what makes
    asking cheap enough to do around every test.
    """
    from agentic_hil.config import absolute_without_symlinks, tool_owned_user_roots

    return (absolute_without_symlinks(Path.home()), *tool_owned_user_roots())


# The same question answered here, at import time, before the first fixture has
# redirected anything: these are the operator's own directories, and no test may
# resolve back onto them. Compared case-insensitively because that is how Windows
# compares paths, and a test reaches these variables by writing strings into them.
REAL_USER_PATHS = frozenset(os.path.normcase(str(path)) for path in user_level_paths())


def assert_still_sandboxed(when: str) -> None:
    """Fail while the damage is still one CLI call away, naming who did it.

    The suite's whole safety story is that `isolated_config_environment` moves
    every user-level path into the sandbox, and until #270 any test could revert
    that with one line: `monkeypatch.undo()` in a test body reverted the redirect
    along with everything else recorded on the test's own instance. The three CLI
    calls that followed ran against the developer's real profile, and the
    `uninstall` among them removed all three installed agent skills and their MCP
    registrations. Nothing in the run looked wrong, which is why this has to be
    asked here rather than inferred later from what a profile is missing.

    The comparison is equality with the paths captured before the first redirect,
    not a prefix test against the sandbox: a test is free to point HOME at
    `tmp_path`, or through `pytester` at pytest's own basetemp, and those are
    isolated too. Only landing back on the real thing is the failure. On Windows
    a prefix test would also refuse the entire suite, because the per-user Temp
    root that holds the sandbox is itself inside the real profile.
    """
    escaped = [path for path in user_level_paths() if os.path.normcase(str(path)) in REAL_USER_PATHS]
    if not escaped:
        return
    raise SandboxEscaped(
        f"User-level paths point at the operator's own profile {when}: "
        + ", ".join(str(path) for path in escaped)
        + ". Something reverted the redirect `isolated_config_environment` installs, so an `agentic-hil` call from here"
        + " writes to, or uninstalls from, the real profile instead of the sandbox."
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Check the sandbox on both sides of every test body.

    Before it runs, because a fixture can lose the redirect as easily as a test
    can, and the test about to run against the real profile is the one worth
    naming. After it has run, because that is where a test body's own doing shows
    up. In a `finally`, because a test that broke the isolation and then failed
    for its own reason is exactly the case where the breach is the news.
    """
    assert_still_sandboxed(f"before {item.nodeid} ran")
    try:
        result = yield
    finally:
        assert_still_sandboxed(f"after {item.nodeid} ran")
    return result


ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_OPENOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_openocd_no_target.py"
FAKE_OPENOCD_MISSING_CFG = ROOT / "tests" / "fixtures" / "fake_openocd_missing_cfg.py"
FAKE_OPENOCD_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_unconfirmed.py"
FAKE_OPENOCD_POST_INIT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_post_init_unconfirmed.py"
FAKE_OPENOCD_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_openocd_erase_refused.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"
FAKE_STLINK_READ_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_read_unconfirmed.py"
FAKE_STLINK_SHORT_READ = ROOT / "tests" / "fixtures" / "fake_stlink_short_read.py"
FAKE_STLINK_HALT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_halt_unconfirmed.py"
FAKE_STLINK_PARTIAL_CONFIRMATION = ROOT / "tests" / "fixtures" / "fake_stlink_partial_confirmation.py"
FAKE_STLINK_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_stlink_no_target.py"
FAKE_STLINK_NO_PROBE = ROOT / "tests" / "fixtures" / "fake_stlink_no_probe.py"
FAKE_STLINK_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_stlink_erase_refused.py"
FAKE_STLINK_ERASE_MID_FLASH = ROOT / "tests" / "fixtures" / "fake_stlink_erase_mid_flash.py"
FAKE_PYOCD = ROOT / "tests" / "fixtures" / "fake_pyocd.py"
FAKE_PYOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_pyocd_no_target.py"
FAKE_PYOCD_SILENT_READ = ROOT / "tests" / "fixtures" / "fake_pyocd_silent_read.py"
FAKE_PYOCD_UNKNOWN_TARGET = ROOT / "tests" / "fixtures" / "fake_pyocd_unknown_target.py"
FAKE_PYOCD_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_pyocd_erase_refused.py"
FAKE_GDB = ROOT / "tests" / "fixtures" / "fake_gdb.py"



@pytest.fixture(autouse=True)
def isolated_config_environment(
    tmp_path: Path,
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
    # Deliberately not the `monkeypatch` fixture. That instance is the test's
    # own, and `monkeypatch.undo()` in a test body reverts everything recorded on
    # it, this redirect included; one test did exactly that, and the CLI calls
    # that followed installed into and then uninstalled from the developer's real
    # profile (#270). An instance the fixture owns is out of reach of anything a
    # test does to its own, and the finalizer registered for it runs after the
    # test's monkeypatch has already rolled its own changes back onto these
    # values, so the environment still unwinds in the order it was built.
    isolation = pytest.MonkeyPatch()
    request.addfinalizer(isolation.undo)
    home_root.mkdir(parents=True)
    temp_root.mkdir(parents=True)
    isolation.setenv("HOME", str(home_root))
    isolation.setenv("USERPROFILE", str(home_root))
    isolation.setenv("APPDATA", str(config_root))
    isolation.setenv("XDG_CONFIG_HOME", str(config_root))
    isolation.setenv("XDG_CACHE_HOME", str(home_root / ".cache"))
    isolation.setenv("XDG_DATA_HOME", str(home_root / ".local" / "share"))
    isolation.setenv("LOCALAPPDATA", str(state_root))
    isolation.setenv("XDG_STATE_HOME", str(state_root))
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
        isolation.setenv(variable, str(temp_root))
    isolation.setattr(tempfile, "tempdir", str(temp_root))
    # The width every rendered result wraps to is redirected for the reason the
    # paths above are: it belongs to the runner, and a rendering test compares
    # text. `shutil.get_terminal_size` reads COLUMNS and LINES before it asks the
    # device, and nothing sets either for a pytest process. A pytest-xdist worker
    # on Linux is handed COLUMNS=80 and LINES=24 all the same: importing
    # `readline` in the parent has GNU readline write its own 80x24 default into
    # the C environment with `setenv`, where the parent's own `os.environ`
    # snapshot never sees it and every child it spawns inherits it. So one
    # document wrapped to 78 columns under `-n auto` and to 86 in a single
    # process, on Linux alone, and an assertion counting a command in the prose
    # passed or failed on how the suite had been started (#390). Pinned to the
    # width the module falls back to when nothing answers, so the rendering is
    # the same everywhere and is nobody's terminal.
    isolation.setenv("COLUMNS", "88")
    isolation.setenv("LINES", "24")
    isolation.delenv("AGENTIC_HIL_CONFIG", raising=False)
    return config_root


@pytest.fixture(autouse=True)
def _no_host_stm32_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap discovery finds no toolchain unless a test hands it one.

    `agentic-hil init` reads the attached bench whatever else is in the workspace,
    so any test that runs it would otherwise spawn whatever STM32CubeProgrammer is
    installed on the machine running the suite and connect to whatever is plugged
    into it: passing on a bare CI container and doing real hardware I/O on a
    developer's bench. A test that wants a toolchain patches this name itself, and
    that patch runs after this one.

    Both toolchains, because discovery has two ways in. With no
    STM32CubeProgrammer it enumerates ST-Link probes out of the host's USB serial
    inventory and drives them with OpenOCD, so a suite that hid only ST's CLI
    would take the fallback path on any machine with `openocd` on PATH and the
    skeleton path everywhere else: the same assertions answering differently per
    developer, which is what this fixture exists to prevent."""
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: None)


@pytest.fixture(autouse=True)
def _no_host_gdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap discovery finds no GDB unless a test hands it one.

    The same rule as the toolchain above, for the same reason: discovery now
    reports the GDB this host answers with, and generation and `adopt-hardware`
    both write it. Left alone, every assertion about what a generated file
    contains or what adoption has left to carry would depend on whether the
    machine running the suite happens to have `arm-none-eabi-gdb` installed,
    which is true on a firmware developer's bench and false in a CI container. A
    test that wants a GDB patches this name itself."""
    monkeypatch.setattr("agentic_hil.bootstrap.autodetected_gdb", lambda: None)


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
    # Omitted from the written entry by default, so the bulk of the suite keeps
    # exercising the file that never named a connect mode and is read at the
    # default. A test that wants the key writes it.
    connect_mode: str | None = None,
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
        **({"connect_mode": connect_mode} if connect_mode is not None else {}),
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
    "allow_debug_execution": True,
    "allow_com_read": True,
    "allow_com_write": True,
    "allow_can_read": True,
    "allow_can_write": True,
    "allow_raw_debugger_commands": False,
    "allow_mass_erase": False,
}
READ_PERMISSION_FLAGS = frozenset({"allow_probe", "allow_read"})
SECTION_GRANTS = {
    "debuggers": {
        "allow_probe": "allow_probe",
        "allow_flash": "allow_flash",
        "allow_reset": "allow_reset",
        "allow_debug_execution": "allow_debug_execution",
        "allow_raw_debugger_commands": "allow_raw_debugger_commands",
        "allow_mass_erase": "allow_mass_erase",
    },
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
