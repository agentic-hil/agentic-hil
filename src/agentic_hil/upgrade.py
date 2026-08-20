"""Replacing this installation with the newest release, once, for two front ends.

`agentic-hil upgrade` and the `server_upgrade` MCP tool ask the same question
of the same machine: which package manager owns this installation, what does it
answer, and did the version actually move.
Everything that decides those three lives here and nowhere else. The two callers
add only what is theirs (the CLI refreshes the agent skills and MCP
registrations afterwards, the MCP tool checks a permission, a bench hold and the
platform first) and neither of them owns a second copy of the manager logic. A
fork here is how one surface learns about a pin the other still reports as
success.

What the two front ends may do also differs, and the difference is the whole of
the MCP tool's design:

**Forward only.** Nothing in this module takes a version. `_upgrade_command`
builds `uv tool upgrade` / `pipx upgrade` / a bare `--upgrade` requirement, all
of which mean "the newest one there is". A version argument on the MCP surface
would make the permission ratchet bypassable through the code: an agent that can
install 0.7.x installs itself weaker rules. Downgrades and exact versions stay at
the shell, with the operator.

**Honest about the restart.** A swap on disk does not change the code this
process is running, so a result that reports the new number as this server's
would be a lie a host cannot check. The vocabulary is the one the staleness block
already uses: `upgraded_on_disk`, `previous_version`, `version`,
`running_version`, `restart_required`.

Two rules are the whole of what both front ends owe an installation, and both
were learned from one report on a pip `--user` machine that was already at the
newest release:

**Nothing is replaced that did not need replacing.** The resolver is asked what
it would do, as the same install with `--dry-run`, before it is allowed to do
anything. An installation that is already current answers `already_current` with
`install_skipped`, and no command capable of removing a file has run.

**A failure leaves the previous installation working, or says that it did not.**
Whether it did is measured, not assumed: after a manager stops, the same import
the console script performs is run through the same interpreter. Still loading
is `upgrade_failed` with `installation_intact`; not loading is
`installation_broken`, with the one command that repairs it and the extras it
must carry, both read before the manager ran.

One failure is answered rather than only reported, and it is the one the
project's own installer has answered since #293: a manager that came back
saying it could not trace the index's certificate to a root it trusts is a
bench behind a TLS-intercepting proxy, and it is retried once against this
machine's own certificate store. Whatever comes of that, the result says what
was tried and carries both attempts' words.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

from agentic_hil import __version__
from agentic_hil.config import ConfigError
from agentic_hil.configwrite import NOT_STARTED
from agentic_hil.knowledge import remediation_fields
from agentic_hil.process import ProcessImage, snapshot_process_images
from agentic_hil.types import AgenticHILConfig, JsonObject

# What the command line calls its own upgrade result, kept apart from the MCP
# tool name: a result that could not say which surface produced it is the wrong
# thing to hand an operator reading an audit trail.
CLI_UPGRADE_TOOL = "agentic_hil_upgrade"
SERVER_UPGRADE = "server_upgrade"

# How far up the process tree the upgrade guard follows launchers before it
# stops looking, and how many holding processes a refusal names before the
# count carries the rest.
_ANCESTOR_WALK_LIMIT = 16
_REPORTED_HOLDER_LIMIT = 10


def _distribution_installer() -> str | None:
    try:
        from importlib.metadata import distribution

        installer = distribution("agentic-hil").read_text("INSTALLER")
    except Exception:
        return None
    return installer.strip().lower() if installer else None


def _normalized_location(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve())).replace("\\", "/").casefold()


def _dedicated_environment_root() -> Path | None:
    """The environment this distribution owns alone, or None when it shares one.

    A uv tool environment and a pipx venv hold Agentic HIL and its dependencies
    and nothing else, so every executable under them belongs to this
    installation. A `pip install --user` prefix or a system Python holds every
    other Python program on the machine as well, and reading its executables as
    ours would refuse an upgrade because some unrelated script is running.
    """
    prefix = Path(sys.prefix)
    location = _normalized_location(prefix)
    if any(marker in location for marker in ("/uv/tools/agentic-hil", "/pipx/venvs/agentic-hil")):
        return prefix
    return None


def _installed_extras() -> tuple[str, ...]:
    """Declared extras whose every requirement is installed alongside this package.

    `uv tool upgrade` and `pipx upgrade` reinstall from the requirement their
    own receipt records, and that requirement already names the extras. The pip
    and `uv pip` paths take the requirement from the command line instead, so
    one that names the bare distribution re-resolves without the extras and
    leaves whatever they installed pinned at whatever version it already had.
    """
    from importlib.metadata import PackageNotFoundError, distribution, version

    try:
        metadata = distribution("agentic-hil").metadata
    except Exception:
        return ()
    declared = [name.strip() for name in metadata.get_all("Provides-Extra") or [] if isinstance(name, str) and name.strip()]
    requirements = [item for item in metadata.get_all("Requires-Dist") or [] if isinstance(item, str)]
    installed: list[str] = []
    for extra in declared:
        marker = re.compile(rf"""extra\s*==\s*['"]{re.escape(extra)}['"]""")
        required = [_requirement_name(item) for item in requirements if ";" in item and marker.search(item.split(";", 1)[1])]
        names = [name for name in required if name]
        if not names:
            continue
        for name in names:
            try:
                version(name)
            except PackageNotFoundError:
                break
        else:
            installed.append(extra)
    return tuple(sorted(installed))


def _requirement_name(requirement: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1) if match else None


def _upgrade_requirement() -> str:
    extras = _installed_extras()
    return f"agentic-hil[{','.join(extras)}]" if extras else "agentic-hil"


# `uv tool upgrade` exits 0 when there was nothing it could do, and an exact
# version pin in the requirement it recorded is one of the ways there is
# nothing to do. It writes the reason on stderr in prose, so the pin is read
# out of the text and never out of the return code. Measured on the reporter's
# box: "hint: `agentic-hil` is pinned to `0.7.1` (installed with an exact
# version pin); reinstall with `uv tool install agentic-hil@latest` to upgrade
# to a new version." A future wording that matches neither phrase falls through
# to the already-current answer, which claims no upgrade happened — never a
# false success, and never a refusal for a machine that is simply up to date.
_EXACT_PIN_MARKERS = ("is pinned to", "exact version pin")
_PINNED_AT = re.compile(r"pinned to [`'\"]?([0-9][^`'\"\s,;)]*)")


def _manager_output(install_result: JsonObject) -> str:
    return " ".join(str(install_result.get(stream, "")) for stream in ("stdout", "stderr"))


def _manager_reports_exact_pin(install_result: JsonObject) -> bool:
    text = _manager_output(install_result).lower()
    return any(marker in text for marker in _EXACT_PIN_MARKERS)


def _pinned_version(install_result: JsonObject) -> str | None:
    match = _PINNED_AT.search(_manager_output(install_result))
    return match.group(1) if match else None


def _unpinned_reinstall_command(manager: str, command: list[str]) -> str | None:
    """The command that clears a recorded exact pin and keeps the installed extras.

    Only a manager that reinstalls from a requirement of its own can hold an
    installation at one version, which is `uv tool` and `pipx`; the `uv pip` and
    `pip` paths take the requirement from the command line this program builds,
    and that one never pins. None for those, and a pin nobody can name a fix for
    is reported as the plain unchanged outcome instead.

    The extras come from `_installed_extras`, read off the distribution that is
    actually installed. `uv`'s own hint names the bare distribution, and `uv`
    records a requirement literally, so a reader who follows that hint loses
    whatever `[can]` or `[pyocd]` brought in -- on a bench with a CAN adapter,
    silently.
    """
    requirement = _upgrade_requirement()
    if manager == "pipx":
        return f'pipx install --force "{requirement}"'
    if command[1:3] == ["tool", "upgrade"]:
        return f'uv tool install "{requirement}@latest"'
    return None


def _user_site_installation() -> bool:
    """Whether the distribution about to be replaced lives in this user's own site.

    `pip install --upgrade` uninstalls the distribution wherever it finds it and
    writes the replacement into the *default* scheme for the interpreter it runs
    under. On a `pip install --user` installation those are two different
    directories: the old copy is removed from the per-user site and the new one
    is written to the system site, so a failure in between leaves the machine
    with the console script that was never in the package's own directory and no
    package behind it. That is exactly the reported end state, a live
    `agentic-hil` shim over a missing `agentic_hil`, and it is reached without
    anything here asking for `--force-reinstall` or an uninstall of its own.

    Passing `--user` keeps both halves in one scheme, which is the in-place
    replacement pip is safe at: the new files are unpacked first and the old
    ones are only released once they are.

    False inside a virtual environment whatever else is true, because pip
    refuses `--user` there outright, and false whenever the location cannot be
    read, because a guess that adds the flag on a system installation would
    scatter the package into a per-user directory nothing on PATH points at.
    """
    if sys.prefix != sys.base_prefix:
        return False
    try:
        from importlib.metadata import distribution

        located = distribution("agentic-hil").locate_file("")
    except Exception:
        return False
    try:
        user_site = sysconfig.get_path("purelib", f"{os.name}_user")
    except (KeyError, OSError):
        return False
    if located is None or not user_site:
        return False
    return _normalized_location(located) == _normalized_location(user_site)


def _pip_upgrade_command() -> list[str]:
    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if _user_site_installation():
        command.append("--user")
    return [*command, _upgrade_requirement()]


def owning_manager() -> str:
    """Which package manager holds the installation this process is running out of.

    Asked once and answered here, because three commands act on the answer: the
    upgrade that runs a manager, the reinstall line a pinned or broken
    installation is handed, and the removal line `agentic-hil uninstall` ends on.
    Two answers to it would send an operator to rebuild or empty an environment a
    different manager is holding.

    The prefix decides first, because a uv tool environment and a pipx venv are
    recognisable from the path this interpreter lives in whatever any receipt
    says. Only then the receipt, which is what tells a `uv pip` installation from
    a plain `pip` one inside an ordinary environment. It reads no PATH: whether
    the manager is reachable right now is a separate question, and one that only
    a caller about to *run* it has to ask.
    """
    prefix = _normalized_location(sys.prefix)
    if "/uv/tools/agentic-hil" in prefix:
        return "uv-tool"
    if "/pipx/venvs/agentic-hil" in prefix:
        return "pipx"
    if _distribution_installer() == "uv":
        return "uv-pip"
    return "pip"


def _upgrade_command() -> tuple[str, list[str]]:
    """Select the manager that owns the running installation, never another PATH copy."""
    manager = owning_manager()
    if manager in {"uv-tool", "uv-pip"}:
        uv = shutil.which("uv")
        if uv is None:
            raise ConfigError("upgrade_manager_not_found", "This installation is managed by uv, but uv is not on PATH.", {"manager": "uv", "python": sys.executable})
        if manager == "uv-tool":
            return "uv", [uv, "tool", "upgrade", "agentic-hil"]
        return "uv", [uv, "pip", "install", "--python", sys.executable, "--upgrade", _upgrade_requirement()]
    if manager == "pipx":
        pipx = shutil.which("pipx")
        if pipx is None:
            raise ConfigError("upgrade_manager_not_found", "This installation is managed by pipx, but pipx is not on PATH.", {"manager": "pipx", "python": sys.executable})
        return "pipx", [pipx, "upgrade", "agentic-hil"]
    return "pip", _pip_upgrade_command()


def reinstall_command_with_extras(extras: tuple[str, ...]) -> str:
    """The line that reinstalls this installation carrying exactly these extras.

    Named and never run, like every other reinstall command this package
    produces: which requirement a machine records is the operator's decision.

    The same four answers `owning_manager` gives the upgrade itself, so a machine
    is never told to rebuild an environment a different manager is holding. It
    differs in taking no PATH lookup: this is a line to print, and a `uv` that is
    missing right now says nothing about whether the operator will have one when
    they run it.
    """
    requirement = f"agentic-hil[{','.join(extras)}]" if extras else "agentic-hil"
    manager = owning_manager()
    if manager == "uv-tool":
        return f'uv tool install "{requirement}@latest"'
    if manager == "pipx":
        return f'pipx install --force "{requirement}"'
    if manager == "uv-pip":
        return f'uv pip install --python "{sys.executable}" --upgrade "{requirement}"'
    scope = " --user" if _user_site_installation() else ""
    return f'"{sys.executable}" -m pip install --upgrade{scope} "{requirement}"'


def removal_command() -> str:
    """The line that removes this installation, from the manager that owns it.

    Named and never run, and here that is not a policy but a fact about the
    process: a package cannot delete the files it is executing out of, so the
    last step of `agentic-hil uninstall` is a line for the operator's shell and
    could not be anything else. On Windows it could not even be a subprocess of
    this one, because the image this interpreter has mapped stays undeletable
    until it exits.

    The same four answers as the upgrade and the reinstall line, through
    `owning_manager`, and no extras on any of them: a requirement names what to
    install, and a removal takes the distribution whole whatever it was installed
    with. No `--yes` either. The line is read before it is run, and a removal
    that asks once is the right shape for one somebody pasted.
    """
    manager = owning_manager()
    if manager == "uv-tool":
        return "uv tool uninstall agentic-hil"
    if manager == "pipx":
        return "pipx uninstall agentic-hil"
    if manager == "uv-pip":
        return f'uv pip uninstall --python "{sys.executable}" agentic-hil'
    return f'"{sys.executable}" -m pip uninstall agentic-hil'


def installed_package_directory() -> str | None:
    """Where the importable package sits, or None when that cannot be read.

    For the one check a removal cannot perform on its own. A `pip uninstall`
    removes the files it has a record of and leaves whatever it does not, and a
    `__pycache__` left behind under this directory keeps `import agentic_hil`
    succeeding afterwards as an empty namespace package. That was observed on a
    real machine, and it is why a reinstall could look like the only way back to
    a working command. Naming the directory lets an operator look, once the
    manager has run and this process is gone.

    None for an editable or source installation, whatever `locate_file`
    answers. Contrary to what a first read of `locate_file` suggests, that is
    not empty ground for one: `locate_file("agentic_hil")` resolves straight to
    the checkout's own source directory for an editable install and
    `Path(...).is_dir()` is true of it, exactly this project's own dev
    container included. Checked against where this interpreter's site-packages
    actually are rather than against `direct_url.json`'s `dir_info.editable`,
    because that field is a PEP 660 marker and this project's own editable
    install is the older `setup.py develop` / `.egg-info` form, which writes no
    `direct_url.json` at all: the checkout sits directly on `sys.path` with
    nothing copied anywhere. A location outside every site-packages directory
    is not a wheel's installed copy under either scheme, so naming it here
    would point an operator's removal at their project source instead of a
    leftover cache.
    """
    with suppress(Exception):
        from importlib.metadata import distribution

        located = distribution("agentic-hil").locate_file("agentic_hil")
        if located is not None and Path(located).is_dir() and _inside_site_packages(located):
            return str(located)
    return None


def _inside_site_packages(location: str | Path) -> bool:
    site_dirs = []
    for scheme_key in ("purelib", "platlib"):
        with suppress(KeyError, OSError):
            site_dirs.append(sysconfig.get_path(scheme_key))
    with suppress(KeyError, OSError):
        site_dirs.append(sysconfig.get_path("purelib", f"{os.name}_user"))
    normalized = _normalized_location(location)
    return any(site_dir and normalized.startswith(_normalized_location(site_dir) + "/") for site_dir in site_dirs)


def _host_rmdir_refuses_nonempty_directories() -> bool:
    """Whether this host's `rmdir` already has the behaviour `uninstall` promises.

    POSIX's `rmdir` exits non-zero on a non-empty directory and touches
    nothing. Windows has no equivalent through the shell this project's own
    installer targets (`install.ps1`, PowerShell): `rmdir`/`rd` there is an
    alias for `Remove-Item`, which does not refuse a non-empty directory the
    same way -- it can prompt to remove it and everything still inside it
    instead, which is exactly what this step tells an operator it will never
    do. A function rather than a constant, so a test can exercise either
    branch on whichever host the suite happens to run on -- the same seam
    `_host_locks_running_files` uses below for the same platform split.
    """
    return os.name != "nt"


def empty_directory_removal_command(directory: str) -> str:
    """A copy-paste command that removes `directory` only if nothing is left inside it.

    Quoted either way: an unquoted path containing spaces splits into several
    shell arguments and leaves the stale directory behind with no error to say
    so.

    POSIX's own `rmdir` already refuses a non-empty directory outright, so
    quoting it for the shell is the whole of what is needed there.
    `[System.IO.Directory]::Delete(path, $false)` is PowerShell's equivalent --
    the `recursive: false` overload throws `IOException` on a non-empty
    directory instead of the prompt-then-delete-everything `Remove-Item`/`rmdir`
    give on that host.
    """
    if _host_rmdir_refuses_nonempty_directories():
        return f"rmdir {shlex.quote(directory)}"
    literal = directory.replace("'", "''")
    return f"[System.IO.Directory]::Delete('{literal}', $false)"


# Which declared extra a configured capability needs, and which entries need it.
# Only `can` is claimed, because only `can` is decidable from the configuration
# alone: a `can_buses` entry on any adapter but `process` opens the bus through
# python-can (can.open_adapter), so the requirement is stated by the file rather
# than guessed. A `pyocd` debugger is deliberately absent — the backend runs an
# executable the configuration names by path, which an operator may perfectly
# well have installed outside this environment, so reading a missing extra as a
# broken bench there would be a false alarm on a working setup.
def configured_extra_requirements(config: AgenticHILConfig) -> dict[str, list[str]]:
    direct_can = sorted(bus_id for bus_id, bus in config.can_buses.items() if bus.adapter != "process")
    return {"can": direct_can} if direct_can else {}


def missing_configured_extras(config: AgenticHILConfig) -> JsonObject | None:
    """The contradiction between an installation and its own configuration, or None.

    A configuration that declares CAN buses on an installation without
    python-can is a bench that cannot do what it says it does, and until now the
    first `can_session_start` was what found out — hours after the upgrade that
    caused it, and in a result about a bus rather than about the installation.
    `uv tool install --upgrade` *replaces* the recorded requirement rather than
    raising it, so a bench installed as `agentic-hil[can]` came back without the
    extra and nothing said so.

    Read off `_installed_extras`, which asks the installed distribution rather
    than trying an import: the question is what this installation has, and that
    is also what decides the reinstall command. The command names the union — the
    extras that are here plus the ones the configuration needs — because a
    reinstall that carried only the missing one would take the others away.
    """
    needed = configured_extra_requirements(config)
    if not needed:
        return None
    installed = _installed_extras()
    missing = {extra: entries for extra, entries in needed.items() if extra not in installed}
    if not missing:
        return None
    names = sorted(missing)
    return {
        "missing_extras": names,
        "installed_extras": list(installed),
        "configured_entries": {extra: missing[extra] for extra in names},
        "summary": (
            f"This configuration declares {', '.join(f'can_buses.{entry}' for extra in names for entry in missing[extra])}, "
            f"and this installation does not carry the {', '.join(f'[{name}]' for name in names)} extra those entries need. "
            "The bench cannot open those buses until it is reinstalled with them; `reinstall_command` is the line that "
            "does it and keeps the extras already installed."
        ),
        "reinstall_command": reinstall_command_with_extras(tuple(sorted({*installed, *names}))),
    }


def _installation_console_scripts() -> tuple[str, ...]:
    """Where this distribution's own console script can sit for this interpreter.

    A virtual environment puts it beside the interpreter; a system or `--user`
    installation puts it in that scheme's script directory instead. Only these
    exact files are attributable to this distribution, which is what a shared
    prefix needs: the interpreter there runs every other Python program on the
    machine too.
    """
    name = "agentic-hil.exe" if os.name == "nt" else "agentic-hil"
    directories = [Path(sys.executable).parent, Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")]
    for scheme in (None, f"{os.name}_user"):
        with suppress(KeyError, OSError):
            directories.append(Path(sysconfig.get_path("scripts") if scheme is None else sysconfig.get_path("scripts", scheme)))
    return tuple(dict.fromkeys(_normalized_location(directory / name) for directory in directories))


def _belongs_to_installation(image: str, owned_prefix: str | None, scripts: tuple[str, ...]) -> bool:
    location = _normalized_location(image)
    if owned_prefix is not None and location.startswith(owned_prefix):
        return True
    return location in scripts


def _upgrading_process_and_its_launchers(by_pid: dict[int, ProcessImage], owned_prefix: str | None, scripts: tuple[str, ...]) -> set[int]:
    """This process, plus the parents inside the installation that started it.

    `agentic-hil upgrade` runs out of the very installation it replaces, so it
    would otherwise report itself and refuse every upgrade there is. Measured on
    Windows: the interpreter executing this code has its image at the base
    Python outside the environment, and the process that actually holds
    `Scripts\\python.exe` open is its parent -- a launcher the environment
    installs, with the console script another level above it. Excluding only
    `os.getpid()` would therefore still find a holder every single time.

    Walking up is what makes this safe rather than merely permissive. A second
    Agentic HIL -- the MCP server the agent host started, which is the process
    this check exists to find -- is a sibling of this one, never one of its
    parents, so no ancestor walk can reach it.
    """
    excluded = {os.getpid()}
    current = by_pid.get(os.getpid())
    for _ in range(_ANCESTOR_WALK_LIMIT):
        if current is None:
            break
        parent = by_pid.get(current.parent_pid)
        if parent is None or parent.pid in excluded:
            break
        if not _belongs_to_installation(parent.image, owned_prefix, scripts):
            break
        if parent.created_ns > current.created_ns:
            # A pid is reused once its process exits. Nothing can be younger
            # than the child it started, so this entry is an unrelated process
            # that inherited the number, not the launcher we came through.
            break
        excluded.add(parent.pid)
        current = parent
    return excluded


def _processes_holding_installation() -> list[JsonObject]:
    """Processes running out of the installation an upgrade is about to replace.

    Empty on every platform but Windows, and deliberately so. Elsewhere a
    package manager unlinks the old files while the processes using them keep
    reading their own copies, so the upgrade completes and nothing is lost.
    Windows refuses to delete a file that is mapped as a running image, and a
    manager that removes the environment before it rebuilds it then leaves
    neither the old installation nor the new one.
    """
    snapshot = snapshot_process_images()
    if snapshot is None:
        return []
    owned_root = _dedicated_environment_root()
    owned_prefix = _normalized_location(owned_root).rstrip("/") + "/" if owned_root is not None else None
    scripts = _installation_console_scripts()
    excluded = _upgrading_process_and_its_launchers({entry.pid: entry for entry in snapshot}, owned_prefix, scripts)
    holders = [
        {"pid": entry.pid, "image": entry.image}
        for entry in snapshot
        if entry.pid not in excluded and _belongs_to_installation(entry.image, owned_prefix, scripts)
    ]
    return sorted(holders, key=lambda holder: holder["pid"])


def _run_upgrade_process(command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """One child process, output captured, wait bounded.

    `env` is the whole environment the child is given, and `None` is the
    inheritance every call but the certificate retry wants. The retry hands in a
    copy of this process's environment with one variable added rather than
    exporting that variable here: `os.environ` belongs to the process the
    operator started, and a module that wrote to it would change every later
    child of this run and every caller that shares the interpreter.
    """
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=False)


def _process_result(completed: subprocess.CompletedProcess[str]) -> JsonObject:
    result: JsonObject = {"returncode": completed.returncode}
    if completed.stdout.strip():
        result["stdout"] = completed.stdout.strip()
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    return result


# ---------------------------------------------------------------------------
# The TLS-intercepting proxy, met here the way the installer already meets it.
#
# A bench behind a proxy that re-signs TLS gets `invalid peer certificate:
# UnknownIssuer` out of uv, because uv validates against roots compiled into its
# own binary while curl and the system package manager on the same host read the
# machine's store and keep working. That is what makes the cause recognisable,
# and `install.sh` has recognised it since #293: it retries the manager once
# against this machine's own store and says what it did. `agentic-hil upgrade`
# runs the same managers against the same index, so it recognises the same thing
# and answers it the same way rather than handing the operator a certificate
# chain and the job of re-deriving a remedy this project already encodes.
#
# The two lists below are a deliberate mirror rather than a shared definition,
# and the sibling copies are `trust_failure()` and `system_cert_bundle()` in
# install.sh (install.ps1 spells the same signatures as one regular expression in
# `Test-TrustFailure`). They cannot be one copy: the installer runs on a machine
# where this package does not exist yet, so it can read nothing from here, and
# this module cannot call a POSIX shell function. A change to either list is a
# change to all of them.

# The patterns of `trust_failure()` in install.sh, in its order and its casing:
# what a chain that ends outside the manager's own roots looks like from uv
# (rustls), pip (OpenSSL) and curl. A failure that says none of this is a failure
# about something else, and is never retried.
_TRUST_FAILURE_SIGNATURES = (
    "invalid peer certificate",
    "UnknownIssuer",
    "self-signed certificate",
    "self signed certificate",
    "certificate verify failed",
    "CERTIFICATE_VERIFY_FAILED",
    "unable to get local issuer certificate",
)

# The candidates `system_cert_bundle()` in install.sh walks, in its order: the
# first readable one is the file pip is pointed at. uv needs no such list because
# it takes a switch, and pip takes a file. None of these exists on Windows, which
# is why `install.ps1` leaves `PIP_CERT` to the operator there and why this list
# simply finds nothing on that host.
_SYSTEM_CERT_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)

# The one thing that fixes a proxy neither the manager's roots nor this machine's
# store trusts. Named as a step and never performed: it writes to a trust store,
# which is the operator's, and the alternative an impatient reader reaches for is
# a switch that turns verification off, which this project offers nowhere.
_INSTALL_THE_PROXY_CA = (
    "Install the proxy's own CA certificate into this machine's certificate store. Until that is done there is "
    "nothing this command can be pointed at that trusts what the proxy presents, and no switch that turns "
    "verification off is a fallback. TROUBLESHOOTING.md section 1 is the rest of it."
)


class _SystemStoreAttempt(NamedTuple):
    """The one environment change a second attempt would make, or why there is none.

    Exactly one half is filled. `variable` and `value` are what the child gets
    that the first attempt did not; the other three are what a result owes an
    operator when there is nothing new to try, because "retried and it failed
    too" and "not retried, and here is why" are different answers and only one of
    them is ever true. `no_attempt_brief` says the same as `no_attempt` in one
    clause, for the field rather than for the paragraph.
    """

    variable: str = ""
    value: str = ""
    no_attempt: str = ""
    no_attempt_brief: str = ""
    next_step: str = ""


class _CertificateNote(NamedTuple):
    """What a run that met a proxy adds to whatever result it ends in.

    `said` is the prose the summary gains, because a caller that reads one line
    of a result reads that one. `fields` are what goes on the document beside it:
    `certificates`, which is the same fact in one sentence and exists because two
    surfaces rewrite the summary and would otherwise drop it, and `next_steps`.
    Both empty is a run that met no proxy at all.
    """

    said: str
    fields: JsonObject


def _trust_failure(output: str) -> bool:
    """Whether a manager's own words name a certificate it could not trace to a root."""
    return any(signature in output for signature in _TRUST_FAILURE_SIGNATURES)


def _system_cert_bundle() -> str | None:
    """The first readable bundle of the installer's list, or None where there is none."""
    for candidate in _SYSTEM_CERT_BUNDLES:
        with suppress(OSError):
            if Path(candidate).is_file() and os.access(candidate, os.R_OK):
                return candidate
    return None


def _system_store_attempt(manager: str) -> _SystemStoreAttempt:
    """What pointing this manager at this machine's own store would take.

    uv reads a switch (`UV_SYSTEM_CERTS`), pip reads a file (`PIP_CERT`), and
    pipx is a pip: it resolves and installs through the pip inside the
    environment it manages, which reads that variable out of its own environment
    like any other.

    A variable the operator has already exported is left exactly as it is, and
    that is also the whole reason there is no second attempt in that case: it was
    in force for the attempt that just failed, so setting it again would repeat a
    failed run rather than retry it, and overriding it would quietly swap the
    bundle they chose for one this module picked.
    """
    if manager == "uv":
        if os.environ.get("UV_SYSTEM_CERTS", "").strip():
            return _SystemStoreAttempt(
                no_attempt=(
                    "UV_SYSTEM_CERTS is already set in this environment, so uv read this machine's own store on the "
                    "attempt that just failed and a second one would only repeat it."
                ),
                no_attempt_brief="not retried, because UV_SYSTEM_CERTS is already set here and was in force for the attempt that failed",
                next_step=_INSTALL_THE_PROXY_CA,
            )
        return _SystemStoreAttempt("UV_SYSTEM_CERTS", "1")
    if os.environ.get("PIP_CERT", "").strip():
        return _SystemStoreAttempt(
            no_attempt=(
                "PIP_CERT is already set in this environment, so pip read the bundle it names on the attempt that "
                "just failed and a second one would only repeat it."
            ),
            no_attempt_brief="not retried, because PIP_CERT is already set here and was in force for the attempt that failed",
            next_step=_INSTALL_THE_PROXY_CA,
        )
    bundle = _system_cert_bundle()
    if bundle is None:
        return _SystemStoreAttempt(
            no_attempt=(
                f"No certificate bundle was readable at any of {', '.join(_SYSTEM_CERT_BUNDLES)}, which is the list "
                f"the one-line installer searches, so there was nothing to point pip at and no second attempt was made."
            ),
            no_attempt_brief="not retried, because no certificate bundle was found on this host for pip to be pointed at",
            next_step=(
                "Set PIP_CERT to this machine's certificate bundle and run the upgrade again. TROUBLESHOOTING.md "
                "section 1 is the rest of it."
            ),
        )
    return _SystemStoreAttempt("PIP_CERT", bundle)


def _trust_diagnosis(manager: str) -> str:
    """The cause, in the words that let an operator recognise their own network."""
    if manager == "uv":
        return (
            "That is a certificate uv could not trace to a root it carries, which is what a TLS-intercepting proxy "
            "looks like from inside uv: uv validates against roots compiled into its own binary, while curl and the "
            "system package manager on the same host read this machine's own store and keep working."
        )
    return (
        f"That is a certificate {manager} could not trace to a trusted root, which is what a TLS-intercepting proxy "
        f"re-signing this connection looks like from inside it."
    )


def _persistent_export(attempt: _SystemStoreAttempt) -> str:
    """The standing fix TROUBLESHOOTING recommends, so the retry is never needed again."""
    return (
        f"Export {attempt.variable}={attempt.value} in the environment this bench upgrades from, so every later "
        f"upgrade reads this machine's own certificate store without needing a second attempt. TROUBLESHOOTING.md "
        f"section 1 is the rest of it."
    )


def _certificate_note(said: str, brief: str, next_steps: list[str]) -> _CertificateNote:
    """One outcome of meeting a proxy, in the two lengths a result needs it in.

    `said` is a paragraph and `brief` is a clause, because they are read in
    different places: the paragraph goes in the summary a person reads first, and
    the clause goes in a field beside it. The same paragraph in both would be one
    notice printed twice on one screen, which reads as two.
    """
    return _CertificateNote(said, {"certificates": f"{brief[:1].upper()}{brief[1:]}.", "next_steps": next_steps})


def _manager_run(manager: str, command: list[str]) -> tuple[subprocess.CompletedProcess[str], JsonObject, _CertificateNote]:
    """The manager, run once, and run once more when its own words name the proxy.

    Three values: the attempt whose outcome this upgrade reports, that attempt's
    `install` payload, and the certificate fields the result carries, which are
    empty whenever the manager said nothing about a certificate at all.

    The retried attempt is the one reported, and the attempt it replaces is kept
    under `install.certificate_retry.first_attempt` rather than dropped. A
    refusal after both attempts has to carry both managers' own words: the first
    is what names the proxy, and the second is what says this machine's store did
    not help either, and neither one alone is the diagnosis.

    Exactly one extra attempt, and only ever with verification on. There is no
    path through here to `--allow-insecure-host`, `--trusted-host` or any other
    switch that turns checking off, which is the same promise the installer makes.
    """
    first = _run_upgrade_process(command)
    first_result = _process_result(first)
    if first.returncode == 0 or not _trust_failure(_manager_output(first_result)):
        return first, first_result, _CertificateNote("", {})

    diagnosis = _trust_diagnosis(manager)
    attempt = _system_store_attempt(manager)
    if attempt.no_attempt:
        return first, first_result, _certificate_note(f"{diagnosis} {attempt.no_attempt}", attempt.no_attempt_brief, [attempt.next_step])

    retry_record: JsonObject = {"variable": attempt.variable, "value": attempt.value, "first_attempt": first_result}
    retried_with = f"It was retried once with {attempt.variable}={attempt.value} in the manager's environment, so it read this machine's own certificate store, with verification on in both attempts"
    retried_briefly = f"retried once against this machine's own certificate store with {attempt.variable}={attempt.value}"
    try:
        retried = _run_upgrade_process(command, env={**os.environ, attempt.variable: attempt.value})
    except (OSError, subprocess.TimeoutExpired) as error:
        # The retry never produced an outcome, so the first attempt stays the one
        # being reported and the diagnosis it earned is not thrown away with it.
        return (
            first,
            {**first_result, "certificate_retry": {**retry_record, "exception_type": type(error).__name__, "detail": str(error)}},
            _certificate_note(
                f"{diagnosis} {retried_with}, and that second attempt did not finish: {type(error).__name__}.",
                f"{retried_briefly}, and that attempt did not finish ({type(error).__name__})",
                [_persistent_export(attempt)],
            ),
        )

    install_result = {**_process_result(retried), "certificate_retry": retry_record}
    if retried.returncode == 0:
        return (
            retried,
            install_result,
            _certificate_note(
                f"{diagnosis} {retried_with}, and that second attempt is the one that succeeded.",
                f"{retried_briefly}, and that is the attempt that succeeded",
                [_persistent_export(attempt)],
            ),
        )
    return (
        retried,
        install_result,
        _certificate_note(
            f"{diagnosis} {retried_with}, and that attempt failed the same way: the proxy's own CA is missing from "
            f"this machine's store as well, so pointing a manager at that store cannot help until it is there.",
            f"{retried_briefly}, and that attempt failed the same way",
            [_INSTALL_THE_PROXY_CA, _persistent_export(attempt)],
        ),
    )


def _with_certificate_note(outcome: JsonObject, note: _CertificateNote) -> JsonObject:
    """The outcome, plus what happened about certificates, wherever it ended up.

    One place rather than five. A trust failure is answered the same way whether
    the retry then upgraded the machine, found nothing left to do, failed again
    or left an installation that will not load, and what happened belongs in the
    summary on every one of them, because that is the line a person reads first.
    """
    if not note.said:
        return outcome
    summary = str(outcome.get("summary", "")).strip()
    return {**outcome, **note.fields, "summary": f"{summary} {note.said}".strip()}


# ---------------------------------------------------------------------------
# What the manager would do, asked before it is allowed to do anything.
#
# `uv tool upgrade` and `pipx upgrade` answer this inside the manager: each
# reads the requirement it recorded for this installation, finds nothing to
# move, and exits without touching the environment. The two pip-shaped routes
# hand a bare `install --upgrade` to a resolver instead, and that resolver
# decides while it is already replacing files, so "there was nothing to do"
# arrives here as an outcome rather than as a decision. An installation that
# needed nothing has then already been uninstalled, and a failure anywhere in
# that stretch is a failure that had no reason to be risked at all.
#
# The question is asked as the same install with `--dry-run`, not as a separate
# version lookup, because a second opinion that disagrees with the resolver is
# worth nothing: it is the resolver's own answer, on the same requirement, with
# the same index configuration, that decides whether the mutating run happens.


def _resolution_query(manager: str, command: list[str]) -> list[str] | None:
    """The upgrade command turned into a question, or None where there is none."""
    if manager == "pip" and command[1:4] == ["-m", "pip", "install"]:
        # `--report -` puts pip's own resolution on stdout as JSON and `--quiet`
        # leaves nothing else there, so `install: []` is pip stating that it
        # would install nothing. Older pips reject the flag and exit non-zero,
        # which reads as "cannot tell" below and changes nothing.
        return [*command, "--dry-run", "--quiet", "--report", "-"]
    if manager == "uv" and command[1:3] == ["pip", "install"]:
        return [*command, "--dry-run"]
    return None


# What `uv pip install --dry-run` prints when the environment already satisfies
# the requirement: "Resolved N packages ... Checked N packages ... Would make no
# changes". uv publishes no machine-readable report for `uv pip`, so this one is
# read out of the prose the way the exact-pin hint above is. A future wording
# that matches neither this nor pip's report falls through to "cannot tell",
# which runs the upgrade exactly as it ran before rather than calling an
# installation current on a guess.
_UV_PIP_NO_CHANGES = "would make no changes"


def _would_install_nothing(manager: str, command: list[str]) -> tuple[bool, JsonObject | None]:
    """Whether this upgrade would install nothing, decided without installing anything.

    The pair is (answer, what the manager said). A False answer means either
    that there is something to install or that the question could not be put to
    this manager at all, and the two are deliberately not distinguished: both
    lead to the same place, which is running the upgrade as before.
    """
    query = _resolution_query(manager, command)
    if query is None:
        return False, None
    try:
        answered = _run_upgrade_process(query)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    resolution = _process_result(answered)
    if answered.returncode != 0:
        return False, resolution
    if manager == "pip":
        try:
            report = json.loads(answered.stdout)
        except (json.JSONDecodeError, TypeError):
            return False, resolution
        planned = report.get("install") if isinstance(report, dict) else None
        return isinstance(planned, list) and not planned, resolution
    return _UV_PIP_NO_CHANGES in _manager_output(resolution).lower(), resolution


def _nothing_to_install(
    tool: str,
    manager: str,
    command: list[str],
    current_version: str,
    resolution: JsonObject | None,
) -> JsonObject:
    """Already current, answered before the installation could be put at risk.

    The same `already_current` a manager that ran and moved nothing produces, so
    a caller reads one field for one fact, plus `install_skipped` for the part
    that is new: no command capable of replacing this installation was run. Its
    absence on the other outcomes is the signal that one was.
    """
    return {
        "ok": True,
        "tool": tool,
        "already_current": True,
        "summary": (
            f"Agentic HIL is already at {current_version}, which is the newest release {manager} resolves for this "
            f"installation, so nothing was installed and no command that could replace it was run. No restart is needed."
        ),
        "manager": manager,
        "command": command,
        "install_skipped": True,
        "python": sys.executable,
        "previous_version": current_version,
        "version": current_version,
        **({"resolution": resolution} if resolution is not None else {}),
        "restart_required": False,
    }


# ---------------------------------------------------------------------------
# Whether an upgrade that did not finish left an installation behind.


def _loaded_version() -> tuple[JsonObject, str | None]:
    """What this installation's Python still answers, and None when it answers nothing.

    The same import the console script performs, through the same interpreter
    the script's shim runs: a package whose files a half-finished manager run
    removed fails here exactly as the shim fails, which is the difference
    between an upgrade that failed and an installation that is gone.
    """
    command = [sys.executable, "-m", "agentic_hil", "--version"]
    try:
        probed = _run_upgrade_process(command)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "exception_type": type(error).__name__, "detail": str(error)}, None
    verification = _process_result(probed)
    reported = probed.stdout.strip() if probed.returncode == 0 else ""
    return verification, reported or None


def _failed_upgrade(
    tool: str,
    manager: str,
    command: list[str],
    previous_version: str,
    installed_extras: tuple[str, ...],
    reinstall_command: str,
    *,
    summary: str,
    **failure: object,
) -> JsonObject:
    """A manager run that did not finish, and what it left standing.

    An upgrade is allowed to fail; the house rule it may not break is that a
    failure leaves the previous installation working. Whether it did is not a
    thing to assume in either direction, so it is measured: the same import the
    console script does, through the same interpreter, after the manager has
    stopped. There are three ends to tell apart, and the version this loads is
    what tells them apart. A machine that still answers `previous_version` gets
    the plain failure it always got, intact and unreplaced. A machine that
    answers a *different* version was changed on disk before the run stopped, and
    is neither intact nor gone: the report says so, with both numbers, because
    calling that unreplaced would state the opposite of what the probe found. A
    machine that answers nothing is the reported end state, and the one thing it
    must never receive is a failure notice with no mention that the installation
    behind it is gone.
    """
    base: JsonObject = {
        "ok": False,
        "tool": tool,
        "manager": manager,
        "command": command,
        "python": sys.executable,
        "previous_version": previous_version,
        **failure,
    }
    verification, loaded = _loaded_version()
    if loaded == previous_version:
        return {
            **base,
            "error_type": "upgrade_failed",
            "summary": f"{summary} This installation still runs {loaded} and was not replaced.",
            "installation_intact": True,
            "version": loaded,
            "restart_required": False,
            "verification": verification,
        }
    if loaded is not None:
        return _upgrade_changed_on_disk(base, previous_version, loaded, installed_extras, reinstall_command, verification, summary)
    return _installation_broken(base, installed_extras, reinstall_command, verification, summary)


def _upgrade_changed_on_disk(
    base: JsonObject,
    previous_version: str,
    loaded: str,
    installed_extras: tuple[str, ...],
    reinstall_command: str,
    verification: JsonObject,
    summary: str,
) -> JsonObject:
    """The manager failed, yet the files on disk load a different version now.

    The one end of a failed upgrade that is neither intact nor gone. The manager
    exited non-zero, but the package its interpreter now imports answers a
    version other than the one this process runs, so the run replaced files
    before it stopped and left a half-changed tree. Reporting that as intact and
    `was not replaced` would state the opposite of what the probe just found. The
    two numbers are named as they are — the release still running in this
    process, and the one the half-finished run left on disk — and the repair is
    the same known-good reinstall a broken installation gets, because a version a
    failed run left behind is not one to trust into service by restarting onto
    it. `restart_required` is false for that reason: a restart would adopt the
    half-changed tree, which is the outcome the reinstall exists to avoid.
    """
    return {
        **base,
        "error_type": "installation_changed_after_failed_upgrade",
        "installation_intact": False,
        "changed_on_disk": True,
        "summary": (
            f"{summary} The manager reported failure, but this installation's Python now loads {loaded}, not the "
            f"{previous_version} this process is running: the run replaced files on disk before it stopped, so the "
            f"installation is in a half-changed state. The running server is still {previous_version} while the disk "
            f"is {loaded}. Do not restart onto it. Run `reinstall_command` to restore a known installation: {reinstall_command}"
        ),
        "version": loaded,
        "installed_extras": list(installed_extras),
        "reinstall_command": reinstall_command,
        "verification": verification,
        "restart_required": False,
        **remediation_fields("installation_changed_after_failed_upgrade"),
    }


def _installation_broken(
    base: JsonObject,
    installed_extras: tuple[str, ...],
    reinstall_command: str,
    verification: JsonObject,
    summary: str,
) -> JsonObject:
    """The package is gone and the console script is not: name the repair, exactly.

    Reached from a manager that failed and from one that reported success, since
    the fact is the same either way and so is the fix. `reinstall_command` and
    `installed_extras` are read before the manager runs, because the metadata
    they come from is part of what is missing by the time this is written, and a
    command rebuilt from what is left names the bare distribution and silently
    drops the extras the bench was created with.
    """
    return {
        **base,
        "error_type": "installation_broken",
        "installation_broken": True,
        "summary": (
            f"{summary} This installation is now broken: its Python can no longer load `agentic_hil`, while the "
            f"`agentic-hil` console script is still on PATH and will fail with a ModuleNotFoundError on the next call. "
            f"Nothing will work again until `reinstall_command` is run: {reinstall_command}"
        ),
        "installed_extras": list(installed_extras),
        "reinstall_command": reinstall_command,
        "verification": verification,
        "restart_required": False,
        **remediation_fields("installation_broken"),
    }


def _upgrade_changed_nothing(
    tool: str,
    manager: str,
    command: list[str],
    previous_version: str,
    current_version: str,
    install_result: JsonObject,
) -> JsonObject:
    """The two outcomes a package manager reports with the same exit code as success.

    `uv tool upgrade` returns 0 for "upgraded" and for "nothing to do" alike, so
    the return code cannot tell them apart and the version can: this is reached
    only when the installation still runs the version it ran before. Both
    answers here are `ok: false` with `restart_required: false`, because there is
    nothing new to load -- the reported defect was the opposite pair, which sent
    an operator to a restart that reloaded the same release and left them
    believing they had moved.

    Which of the two it is matters, because the next step differs: an
    installation that is already current needs nothing, while one held at an
    exact pin needs a reinstall the operator has to decide on and run.
    """
    base: JsonObject = {
        # Set per branch below: an installation that is already current is a
        # success — nothing to do and nothing wrong — while one held at a pin is
        # not, because the operator wanted a newer release and did not get it.
        # Refusing both would make `agentic-hil upgrade` exit non-zero on every
        # up-to-date machine, which breaks the provisioning scripts that run it
        # unconditionally. The defect was claiming an upgrade that never
        # happened, not reporting that there was none to make.
        "ok": False,
        "tool": tool,
        "manager": manager,
        "command": command,
        "python": sys.executable,
        "previous_version": previous_version,
        "version": current_version,
        "install": install_result,
        "restart_required": False,
    }
    reinstall_command = _unpinned_reinstall_command(manager, command)
    if reinstall_command is not None and _manager_reports_exact_pin(install_result):
        return {
            **base,
            "error_type": "upgrade_blocked_by_pin",
            "summary": (
                f"Agentic HIL was not upgraded: {manager} holds this installation at an exact version pin, so it is "
                f"still {current_version}. Nothing was changed. `reinstall_command` is the line that clears the pin "
                f"and keeps the installed extras; running it is the operator's decision."
            ),
            "pinned_version": _pinned_version(install_result) or current_version,
            "installed_extras": list(_installed_extras()),
            "reinstall_command": reinstall_command,
            **remediation_fields("upgrade_blocked_by_pin"),
        }
    return {
        **base,
        "ok": True,
        "summary": (
            f"Agentic HIL is already at {current_version}; {manager} had nothing to replace. No restart is needed. "
            f"If a newer release was expected, the installation's recorded requirement is what holds it here."
        ),
        "already_current": True,
    }


def replace_installation(*, tool: str) -> JsonObject:
    """Hand this installation to its own package manager and report what moved.

    The whole of the manager interaction and none of either caller's own
    business: no skill refresh, no permission, no bench check. It raises
    `ConfigError("upgrade_manager_not_found")` rather than returning it, because
    that is the one condition where there is no manager to have an outcome from —
    the command line prints it as a refusal like any other, and the MCP tool
    turns it into a result.
    """
    holders = _processes_holding_installation()
    if holders:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "installation_in_use",
            "summary": "Another process is running out of this installation, so upgrading it now would leave no working installation at all. Nothing was changed.",
            "python": sys.executable,
            "installation_root": str(Path(sys.prefix)),
            "held_by": holders[:_REPORTED_HOLDER_LIMIT],
            "held_by_count": len(holders),
            "installed_version": __version__,
            **remediation_fields("installation_in_use"),
        }

    manager, command = _upgrade_command()
    previous_version = __version__
    # Both read before anything runs. They describe the installation as it is
    # now, and the one result that needs them is the one where it is not there
    # any more: rebuilt afterwards from metadata a half-finished manager run
    # removed, the repair command names the bare distribution and takes `[can]`
    # off a bench that had it.
    installed_extras = _installed_extras()
    reinstall_command = reinstall_command_with_extras(installed_extras)

    skipped, resolution = _would_install_nothing(manager, command)
    if skipped:
        return _nothing_to_install(tool, manager, command, previous_version, resolution)

    try:
        installed, install_result, certificates = _manager_run(manager, command)
    except (OSError, subprocess.TimeoutExpired) as error:
        return _failed_upgrade(
            tool,
            manager,
            command,
            previous_version,
            installed_extras,
            reinstall_command,
            summary="Agentic HIL package upgrade could not run.",
            exception_type=type(error).__name__,
            detail=str(error),
        )
    if installed.returncode != 0:
        return _with_certificate_note(
            _failed_upgrade(
                tool,
                manager,
                command,
                previous_version,
                installed_extras,
                reinstall_command,
                summary="Agentic HIL package manager reported an upgrade failure.",
                install=install_result,
            ),
            certificates,
        )

    verification, current_version = _loaded_version()
    if current_version is None:
        # A manager that exited zero and left nothing that loads is the same end
        # state as one that failed, reported the same way: the previous outcome
        # here named the verification step and stopped, which told an operator
        # that something could not be loaded without telling them their
        # installation was gone or what single command brings it back.
        return _with_certificate_note(
            _installation_broken(
                {"ok": False, "tool": tool, "manager": manager, "command": command, "python": sys.executable, "previous_version": previous_version, "install": install_result},
                installed_extras,
                reinstall_command,
                verification,
                "Agentic HIL package manager completed, but the installation it left cannot be loaded.",
            ),
            certificates,
        )

    if current_version == previous_version:
        return _with_certificate_note(_upgrade_changed_nothing(tool, manager, command, previous_version, current_version, install_result), certificates)

    return _with_certificate_note(
        {
            "ok": True,
            "tool": tool,
            "upgraded_on_disk": True,
            "summary": f"Agentic HIL upgraded from {previous_version} to {current_version} on disk.",
            "manager": manager,
            "command": command,
            "python": sys.executable,
            "previous_version": previous_version,
            "version": current_version,
            "install": install_result,
            "restart_required": True,
        },
        certificates,
    )


# ---------------------------------------------------------------------------
# The MCP tool.
#
# Three gates before the shared implementation above, in this order and for this
# reason. The permission is first because it is the operator's standing policy
# and the answer does not depend on anything else — a bench whose
# `allow_upgrade` is false gets the same refusal whatever the platform or the
# hardware is doing. The platform is second because on Windows the answer is
# fixed too: no arrangement of runs or leases makes the call possible, so
# reporting a transient reason there would send a caller to close a run and come
# back for a refusal that was waiting all along. The bench hold is last because
# it is the only one of the three that changes by itself.


def _upgrade_permission_denied(config: AgenticHILConfig) -> JsonObject:
    return {
        "ok": False,
        "tool": SERVER_UPGRADE,
        "error_type": "permission_denied",
        "summary": (
            "Upgrading this installation is denied by permissions.allow_upgrade in the authoritative configuration. "
            "Nothing was changed, and this server still runs the version it started with."
        ),
        "permission": "allow_upgrade",
        "running_version": __version__,
        "restart_required": False,
        "path": config.config_path,
        "workspace_root": config.workspace_root,
        **remediation_fields("permission_denied", "allow_upgrade"),
        **NOT_STARTED,
        "retry_safe": False,
    }


def _host_locks_running_files() -> bool:
    """Whether this platform refuses to replace the files of a running process.

    The property rather than the platform name, and a function rather than a
    constant: it is asked once per call so that the answer is the host's at the
    moment of asking, and it is the one seam a test replaces to exercise either
    side of the branch on whichever machine the suite happens to run on.
    """
    return os.name == "nt"


def _upgrade_cli_only_on_host() -> JsonObject:
    """Windows: a running image cannot be replaced, so the tool says so instead.

    The issue allowed two answers here — a detached helper that swaps once this
    server has exited, or a named refusal — and forbade the third, a swap that
    half happens. The refusal is the one that can be told the truth about. A
    helper outlives the result that announced it: whatever it does afterwards
    happens with nobody left to report it, so the tool would have to promise an
    outcome it cannot observe, and a helper that failed would leave exactly the
    half-replaced environment the promise was made to avoid. `agentic-hil
    upgrade` at a shell is a process that is not the server, so it can watch its
    own manager finish and report what actually happened — and it already has the
    `installation_in_use` guard that names the running server as the holder.
    """
    return {
        "ok": False,
        "tool": SERVER_UPGRADE,
        "error_type": "upgrade_cli_only_on_host",
        "summary": (
            "This host is Windows, where a file mapped as a running image cannot be replaced — and this server is "
            "running out of the installation the upgrade would replace. A package manager that removes the environment "
            "before it rebuilds it fails on that delete and stops in between, leaving neither the old installation nor "
            "the new one, so nothing was attempted. On this host the upgrade is the command line's: `agentic-hil "
            "upgrade` runs as a separate process, refuses while this server still holds the installation, and keeps "
            "the extras this installation was created with."
        ),
        "platform": sys.platform,
        "running_version": __version__,
        "restart_required": False,
        "upgrade_command": "agentic-hil upgrade",
        "installed_extras": list(_installed_extras()),
        **remediation_fields("upgrade_cli_only_on_host"),
        **NOT_STARTED,
        "retry_safe": False,
    }


def _upgrade_in_open_run(bench: JsonObject) -> JsonObject:
    """The open-run refusal, applied to the code rather than to the permissions.

    A run's holds were taken under the release that is running, and replacing
    that release mid-run moves the rules during the run they govern — the same
    move a permission change makes, and refused for the same reason. Read off
    `HardwareCoordinator.status()`'s `bench_held`, which is true for a declared
    run and for a lease alike: a declared run takes the device
    locks and leaves the project lock alone, so asking only about the owner would
    call a held bench free.
    """
    return {
        "ok": False,
        "tool": SERVER_UPGRADE,
        "error_type": "upgrade_in_open_run",
        "summary": (
            "Something is holding this bench right now — a declared run, an open COM or CAN session, or a debug "
            "session — and those holds were taken under the release this server is running. Replacing the code "
            "underneath them would move the rules during the run they govern, so nothing was changed."
        ),
        "running_version": __version__,
        "restart_required": False,
        "held_devices": list(bench.get("held_devices") or []),
        "device_holds": list(bench.get("device_holds") or []),
        "owner_active": bool(bench.get("owner_active")),
        **remediation_fields("upgrade_in_open_run"),
        **NOT_STARTED,
        "retry_safe": True,
    }


# What a caller does after a swap that reached the disk. Deliberately the shape
# `config_stale` uses, because it is the same fact one layer down: what this
# process is answering out of is not what is on disk any more, and exactly one
# thing adopts it.
_RESTART_STEPS = (
    "Ask the operator to restart the MCP server — the one the agent host started for this workspace. That is what "
    "loads the release now on disk, and it is the only thing that does.",
    "Do not use `agentic-hil` at a shell as the check that it worked and then assume this server moved with it. The "
    "command line reads the new code every time it runs and is already current; this server is not, and says so in "
    "`running_version`.",
    "Until that restart, keep working with this server exactly as before. It is the old release, with the old "
    "behaviour and the old refusals, and `running_version` next to `version` is the whole of the difference.",
)


def server_upgrade(config: AgenticHILConfig, bench: JsonObject) -> JsonObject:
    """Lift this installation to the newest release, over MCP, or say why not.

    `bench` is `HardwareCoordinator.status()`. Taken as an argument rather than
    read here, because the coordinator that can answer it is the service's own —
    the one whose leases and declared run are part of the answer.
    """
    if not config.permissions.allow_upgrade:
        return _upgrade_permission_denied(config)
    if _host_locks_running_files():
        return _upgrade_cli_only_on_host()
    if bench.get("bench_held"):
        return _upgrade_in_open_run(bench)
    try:
        result = replace_installation(tool=SERVER_UPGRADE)
    except ConfigError as error:
        return {"tool": SERVER_UPGRADE, **error.to_dict(), "running_version": __version__, "restart_required": False, **NOT_STARTED, "retry_safe": False}
    return _reported_as_running_code(result, config)


def _reported_as_running_code(result: JsonObject, config: AgenticHILConfig) -> JsonObject:
    """Say what this *server* is running, whatever happened on disk.

    The one claim this tool must never make is that the new release is in force.
    Nothing about replacing files changes the code an already-imported process is
    executing, and a host has no way to check the claim, so `running_version` is
    on every outcome — equal to `version` when nothing moved, and deliberately
    behind it when something did.
    """
    reported: JsonObject = {**result, "running_version": __version__}
    extras = missing_configured_extras(config)
    if extras is not None:
        reported["extras_warning"] = extras
    if not result.get("upgraded_on_disk"):
        return reported
    # The summary is rewritten here rather than extended, so anything the shared
    # implementation put in it has to be carried over by name. `certificates` is
    # the one such sentence: an upgrade that only worked because it was retried
    # against this machine's own store must say so on this surface too, or the
    # operator learns nothing about the proxy that will meet the next one.
    certificates = result.get("certificates")
    retry_steps = [step for step in result.get("next_steps") or [] if isinstance(step, str)]
    return {
        **reported,
        "summary": (
            f"Agentic HIL was upgraded from {result['previous_version']} to {result['version']} on disk. This server is "
            f"still running {__version__} and will until it is restarted; nothing about this call changed the code "
            "answering it." + (f" {certificates}" if isinstance(certificates, str) and certificates else "")
        ),
        "next_steps": [*_RESTART_STEPS, *retry_steps],
    }
