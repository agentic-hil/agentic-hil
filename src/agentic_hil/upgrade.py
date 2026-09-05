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
`running_version`, `restart_required`. The same sentence is what every *other*
server running out of this installation gets: it keeps the files it has mapped,
answers with the previous release until its host restarts, and is named under
`restart_required_by` rather than being made a reason to refuse. An upgrade the
operator started at their own shell is never blocked because their bench is
working.

Two rules are the whole of what both front ends owe an installation, and both
were learned from one report on a pip `--user` machine that was already at the
newest release:

**Nothing is replaced that did not need replacing.** The resolver is asked what
it would do, as the same install with `--dry-run`, before it is allowed to do
anything. An installation that is already current answers `already_current` with
`install_skipped`, and no command capable of removing a file has run. That
question reaches the index over the same network the install does, so it carries
the same certificate retry below: a guard that could not be asked behind a proxy
would fall through to the install it exists to prevent.

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
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from time import monotonic
from typing import NamedTuple
from urllib.request import Request, urlopen

from agentic_hil import __version__
from agentic_hil.config import ConfigError
from agentic_hil.configwrite import NOT_STARTED
from agentic_hil.knowledge import INSTALL_THE_PROXY_CA, remediation_fields
from agentic_hil.process import ProcessImage, filetime_epoch_seconds, process_working_directory, snapshot_process_images
from agentic_hil.types import AgenticHILConfig, JsonObject

# What the command line calls its own upgrade result, kept apart from the MCP
# tool name: a result that could not say which surface produced it is the wrong
# thing to hand an operator reading an audit trail.
CLI_UPGRADE_TOOL = "agentic_hil_upgrade"
SERVER_UPGRADE = "server_upgrade"

# How far up the process tree the upgrade follows launchers before it stops
# looking, and how many still-running servers a result names before the count
# carries the rest.
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


def _declared_extras() -> dict[str, list[str]]:
    """Every extra this distribution declares, and the distributions each one pulls in.

    Read to answer which extras are installed (every requirement of the extra is
    present), so the reinstall line an upgrade prints carries exactly the extras
    the machine already has. It is deliberately not used to claim ownership of
    those distributions' console scripts: an isolated manager does not expose a
    dependency's launchers by default, so a same-named file in the shared bin is
    not this installation's to move (review round 0, finding 3, and
    `_entrypoint_names`).
    """
    from importlib.metadata import distribution

    try:
        metadata = distribution("agentic-hil").metadata
    except Exception:
        return {}
    declared = [name.strip() for name in metadata.get_all("Provides-Extra") or [] if isinstance(name, str) and name.strip()]
    requirements = [item for item in metadata.get_all("Requires-Dist") or [] if isinstance(item, str)]
    extras: dict[str, list[str]] = {}
    for extra in declared:
        marker = re.compile(rf"""extra\s*==\s*['"]{re.escape(extra)}['"]""")
        required = [_requirement_name(item) for item in requirements if ";" in item and marker.search(item.split(";", 1)[1])]
        names = [name for name in required if name]
        if names:
            extras[extra] = names
    return extras


def _installed_extras() -> tuple[str, ...]:
    """Declared extras whose every requirement is installed alongside this package.

    `uv tool upgrade` and `pipx upgrade` reinstall from the requirement their
    own receipt records, and that requirement already names the extras. The pip
    and `uv pip` paths take the requirement from the command line instead, so
    one that names the bare distribution re-resolves without the extras and
    leaves whatever they installed pinned at whatever version it already had.
    """
    from importlib.metadata import PackageNotFoundError, version

    installed: list[str] = []
    for extra, names in _declared_extras().items():
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


# ---------------------------------------------------------------------------
# What uv recorded for this installation, read out of uv's own receipt.
#
# `uv tool install` writes `uv-receipt.toml` inside the environment it creates,
# and that file is the whole of what a reinstall has to reproduce: the root
# requirement with its extras, every `--with` package the operator added beside
# it, and the options the install was given. This project's own installer has
# read it since the refresh path was written (`uv_recorded_requirements` in
# install.sh), and this is the same read for the same reason: a reinstall line
# built from the distribution alone silently uninstalls whatever the receipt
# records next to it. A pinned bench that followed such a line lost the pytest
# it had installed with `--with`, from a remediation that promised to keep the
# installation whole.


class _RecordedInstall(NamedTuple):
    """The parts of uv's receipt a reinstall line has to carry forward.

    `with_requirements` are the recorded requirements other than this
    distribution, each already spelled as the PEP 508 string a `--with` takes.
    `python` is the interpreter the install recorded, empty when it recorded
    none. `pin` is the version specifier recorded on this distribution itself,
    exactly as the receipt spells it and empty when the requirement was recorded
    unpinned; it is a record of how the installation was created and never the
    judgement about what holds it back. `exact_pin` is the part of that record
    which can hold a release back, empty for every specifier that cannot.
    `not_replayed` are the recorded requirements a `--with` cannot carry, each
    with the receipt member that stops it, so a line that rebuilds this
    installation names what it leaves behind instead of dropping it in silence.
    `options` is `[tool.options]` whole, because which of its keys matter is the
    reader's question and not this one's.
    """

    with_requirements: tuple[str, ...]
    python: str
    pin: str
    exact_pin: str
    not_replayed: tuple[JsonObject, ...]
    options: JsonObject


def _canonical_requirement_name(name: str) -> str:
    """A distribution name in the one spelling two records can be compared in."""
    return re.sub(r"[-_.]+", "-", name.strip()).casefold()


def _uv_receipt() -> JsonObject | None:
    """uv's own record of how this tool was installed, or None where there is none.

    Only for a `uv tool` installation, because no other manager writes one, and
    read out of the environment rather than out of `uv tool dir`: the receipt
    sits beside the environment this interpreter is running from, so `sys.prefix`
    names it without a manager having to be found, run or waited for.

    Every failure answers None, which is the same answer as "this is not a uv
    tool installation": a receipt that cannot be read is a reinstall line that
    keeps what it can name and nothing more, never a refusal and never a guess.
    """
    if owning_manager() != "uv-tool":
        return None
    try:
        text = (Path(sys.prefix) / "uv-receipt.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 reads it through tomli
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            return None
    try:
        loaded = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _receipt_section(receipt: JsonObject | None, key: str) -> object:
    """One member of the receipt, whether uv nested it under `[tool]` or not.

    uv writes `requirements` and `options` inside `[tool]`, and both spellings
    are accepted because the one thing a reader of somebody else's file must not
    do is fail on a layout that still says what it says.
    """
    if not receipt:
        return None
    tool = receipt.get("tool")
    if isinstance(tool, dict) and key in tool:
        return tool[key]
    return receipt.get(key)


# PEP 508, as much of it as a `--with` value is allowed to be: a distribution
# name, optional extras, an optional version specifier set and an optional
# environment marker. Written out here rather than taken from `packaging`,
# which this distribution does not depend on at runtime and must not start
# depending on for a line it prints.
_REQUIREMENT_NAME_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION_SPECIFIER_PATTERN = r"(?:===|==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9*+!._-]+"
_REQUIREMENT_NAME_ONLY = re.compile(rf"^{_REQUIREMENT_NAME_PATTERN}$")
_VERSION_SPECIFIER_SET = re.compile(rf"^\s*{_VERSION_SPECIFIER_PATTERN}(?:\s*,\s*{_VERSION_SPECIFIER_PATTERN})*\s*$")
# One token of an environment marker: a bare word, a quoted literal with no
# quote of its own inside it, a comparison operator, a bracket or a comma. A
# marker made of nothing but these carries no character a shell reads, which is
# what makes the rendered line safe to paste as well as a real requirement.
_MARKER_TOKEN = re.compile(r"""\s*(?:[A-Za-z0-9_.+*-]+|'[^']*'|"[^"]*"|===|==|!=|<=|>=|~=|<|>|\(|\)|,)\s*""")
# Which receipt members a `--with` can carry. Anything else is a source: a git,
# url, path or editable requirement rebuilds into something other than what uv
# recorded, and a `--with` that changed the requirement would be a different
# installation wearing the same line.
_REPLAYABLE_RECEIPT_KEYS = ("name", "extras", "specifier", "marker")


def _valid_environment_marker(marker: str) -> bool:
    """Whether a marker is made of marker tokens and nothing else.

    Token by token from one end to the other rather than one regular expression
    with a repeated group inside it, so a long unmatchable marker costs one pass
    instead of an exponential search.
    """
    position = 0
    while position < len(marker):
        matched = _MARKER_TOKEN.match(marker, position)
        if matched is None or matched.end() == position:
            return False
        position = matched.end()
    return bool(marker.strip())


def _requirement_string(entry: JsonObject) -> tuple[str, str]:
    """One recorded requirement as a `--with` value, or the receipt key that stops it.

    Exactly one half is filled: the string, or the name of the member that makes
    the entry unspellable. Both a source and a member that is not the thing it
    claims to be end up in the second, and the caller reports them under
    `with_packages_not_replayed` rather than dropping them, because an entry that
    vanished from a line whose own summary says it rebuilds the installation as
    it stands is a package the operator loses without being told.

    A `marker` is replayed rather than refused. uv writes one for
    `--with "pytest; python_version>='3.10'"`, and that requirement is a `--with`
    value exactly as it stands; leaving it out took the package off the
    installation the line claimed to preserve.

    Every part is checked against PEP 508 before it is rendered. A receipt is
    the operator's own file and this is robustness rather than a privilege
    boundary, but the line is meant to be pasted, and a recorded name of
    `pytest" ; echo pwned #` produced exactly that inside the printed command.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not _REQUIREMENT_NAME_ONLY.match(name.strip()):
        return "", "name"
    for key in entry:
        if key not in _REPLAYABLE_RECEIPT_KEYS:
            return "", str(key)
    extras = [item for item in entry.get("extras") or [] if isinstance(item, str) and item.strip()]
    if any(not _REQUIREMENT_NAME_ONLY.match(extra.strip()) for extra in extras):
        return "", "extras"
    rendered = f"{name.strip()}[{','.join(sorted(extra.strip() for extra in extras))}]" if extras else name.strip()
    specifier = entry.get("specifier")
    if isinstance(specifier, str) and specifier.strip():
        if not _VERSION_SPECIFIER_SET.match(specifier):
            return "", "specifier"
        rendered = f"{rendered}{specifier.strip()}"
    marker = entry.get("marker")
    if isinstance(marker, str) and marker.strip():
        if not _valid_environment_marker(marker):
            return "", "marker"
        rendered = f"{rendered}; {marker.strip()}"
    return rendered, ""


def _recorded_install() -> _RecordedInstall:
    """Everything the receipt says about this installation that a reinstall owes it."""
    receipt = _uv_receipt()
    recorded = _receipt_section(receipt, "requirements")
    entries = [entry for entry in recorded if isinstance(entry, dict)] if isinstance(recorded, list) else []
    beside: list[str] = []
    not_replayed: list[JsonObject] = []
    for entry in entries:
        if _canonical_requirement_name(str(entry.get("name") or "")) == "agentic-hil":
            continue
        rendered, stopped_by = _requirement_string(entry)
        if rendered:
            beside.append(rendered)
        else:
            not_replayed.append(
                {
                    "requirement": str(entry.get("name") or "").strip() or "an entry the receipt records with no name",
                    "receipt_key": stopped_by,
                }
            )
    section = _receipt_section(receipt, "options")
    options: JsonObject = {str(key): value for key, value in section.items()} if isinstance(section, dict) else {}
    python = _recorded_python(receipt, options)
    pins = [
        specifier.strip()
        for entry in entries
        if _canonical_requirement_name(str(entry.get("name") or "")) == "agentic-hil"
        and isinstance(specifier := entry.get("specifier"), str)
        and specifier.strip()
    ]
    pin = pins[0] if pins else ""
    return _RecordedInstall(tuple(beside), python, pin, _exact_pin(pin), tuple(not_replayed), options)


def _recorded_python(receipt: JsonObject | None, options: JsonObject) -> str:
    """The interpreter this installation was created with, wherever uv wrote it.

    Both levels, `[tool].python` first, because uv moved it. uv 0.12.9 writes
    `python = "/usr/bin/python3"` directly under `[tool]`, and a reader that
    looked only under `[tool.options]` found nothing on such a receipt: measured
    on a bench, where the reinstall line came out with no `--python` at all while
    the summary beside it promised to rebuild the installation as it stands. A
    line that rebuilds an installation on a different interpreter is a different
    installation wearing the same command.
    """
    for candidate in (_receipt_section(receipt, "python"), options.get("python")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


# A recorded requirement that names one release and nothing else: `==` or `===`
# on every clause, which is what `uv tool install "agentic-hil==0.21.2"` writes.
# Deliberately not `>=`, `~=`, `<`, `>` or `!=`: those are floors and ceilings a
# resolution moves inside, so they hold nothing back that the index would
# otherwise have given.
_EXACT_REQUIREMENT_CLAUSE = re.compile(r"^===?\s*[^\s,=!<>~]+$")


def _exact_pin(specifier: str) -> str:
    """The recorded specifier when it names one release, empty when it does not.

    Only an exact requirement can keep an installation off a release the index
    publishes. A floor cannot: `uv tool install "agentic-hil[can]>=0.21"`, which
    is what AI_AGENT_QUICKSTART.md recommends, resolves to whatever the index
    offers above that number, so reading it as a block refused
    `agentic-hil upgrade` with `upgrade_blocked_by_recorded_option` and exit 1
    on an installation whose own recorded requirement was holding nothing --
    naming that floor as the cause, on a machine a provisioning script runs this
    command on unconditionally.

    Every clause has to be exact, so a `>=0.21,<0.22` range is a range whatever
    the index does, and a specifier this cannot read at all is treated as a
    floor: the safe direction here is the one that never invents a block.
    """
    clauses = [clause.strip() for clause in specifier.split(",") if clause.strip()]
    if not clauses or not all(_EXACT_REQUIREMENT_CLAUSE.match(clause) for clause in clauses):
        return ""
    return specifier.strip()


def _named_series(items: list[str]) -> str:
    """A list of things in the shape a sentence reads them in."""
    if len(items) < 2:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _sentences(*parts: str) -> str:
    """One paragraph out of the parts that have something to say.

    A summary assembled with `f"{a} {b}"` where `b` is conditionally empty leaves
    a double space, or a trailing one, on every run that has nothing to put
    there. Joining what is non-empty says the same thing and cannot.
    """
    return " ".join(part.strip() for part in parts if part and part.strip())


def _installation_shape() -> tuple[tuple[str, ...], _RecordedInstall, JsonObject]:
    """What this installation is made of, and the fields that name it on a result.

    One reader for the two outcomes that hand an operator a reinstall line they
    are meant to run as it stands: the exact-pin refusal, and the one where uv's
    receipt records an option no uv command clears. Both have to name the same
    installation, so both read it here.
    """
    installed_extras = _installed_extras()
    recorded = _recorded_install()
    fields: JsonObject = {"installed_extras": list(installed_extras)}
    if recorded.with_requirements:
        fields["with_packages"] = list(recorded.with_requirements)
    if recorded.python:
        fields["recorded_python"] = recorded.python
    if recorded.not_replayed:
        fields["with_packages_not_replayed"] = list(recorded.not_replayed)
    return installed_extras, recorded, fields


def _carried_by_the_reinstall(recorded: _RecordedInstall) -> str:
    """The clause that says which fields the reinstall line rebuilds from."""
    carried = ["the extras in `installed_extras`"]
    if recorded.with_requirements:
        carried.append("the packages in `with_packages` that uv's receipt records beside it")
    if recorded.python:
        carried.append("the interpreter in `recorded_python`")
    return _named_series(carried)


def _not_carried_by_the_reinstall(recorded: _RecordedInstall) -> str:
    """What the line leaves behind, said out loud, or nothing where it leaves none.

    A requirement recorded as a git, url or path source, or one whose recorded
    parts are not a PEP 508 requirement, cannot be replayed as a `--with`
    without changing what gets installed. Leaving it out is right; leaving it out
    in silence, under a sentence promising the line rebuilds this installation as
    it stands, is how an operator loses a package to a remediation.
    """
    if not recorded.not_replayed:
        return ""
    named = _named_series([f"`{entry['requirement']}` (recorded under `{entry['receipt_key']}`)" for entry in recorded.not_replayed])
    plural = len(recorded.not_replayed) > 1
    return (
        f"It does not carry {named}: {'those requirements are' if plural else 'that requirement is'} recorded in a "
        f"shape a `--with` cannot spell without installing something else, so running the line leaves "
        f"{'them' if plural else 'it'} off. `with_packages_not_replayed` names {'each' if plural else 'it'}."
    )


# `uv tool upgrade` exits 0 when there was nothing it could do, and an exact
# version pin in the requirement it recorded is one of the ways there is
# nothing to do. It writes the reason on stderr in prose, so the pin is read
# out of the text and never out of the return code. Measured on the reporter's
# box: "hint: `agentic-hil` is pinned to `0.7.1` (installed with an exact
# version pin); reinstall with `uv tool install agentic-hil@latest` to upgrade
# to a new version." A future wording that matches neither phrase falls through
# to the already-current answer, which claims no upgrade happened: never a
# false success, and never a refusal for a machine that is simply up to date.
_EXACT_PIN_MARKERS = ("is pinned to", "exact version pin")
_PINNED_AT = re.compile(r"pinned to [`'\"]?([0-9][^`'\"\s,;)]*)")

# The other half of what the manager says about a run that moved nothing: that
# its resolution against the index produced no candidate to install. Read out of
# the same prose as the pin hint above, because `uv tool upgrade` publishes no
# machine-readable report either. A wording that stops matching leaves the pin
# reported as a block, which is where this started and is the safe direction to
# fall: it names a pin that may be holding nothing back, rather than passing over
# one that is.
_NOTHING_TO_UPGRADE = "nothing to upgrade"


def _manager_output(install_result: JsonObject) -> str:
    return " ".join(str(install_result.get(stream, "")) for stream in ("stdout", "stderr"))


def _manager_reports_exact_pin(install_result: JsonObject) -> bool:
    text = _manager_output(install_result).lower()
    return any(marker in text for marker in _EXACT_PIN_MARKERS)


def _pinned_version(install_result: JsonObject) -> str | None:
    match = _PINNED_AT.search(_manager_output(install_result))
    return match.group(1) if match else None


def _pin_holds_nothing_back(install_result: JsonObject, current_version: str, manager: str, command: list[str]) -> tuple[bool, JsonObject | None, _CertificateNote]:
    """Whether the recorded pin names the newest release, holding nothing back.

    Three facts, and the third is the one that carries weight. The manager put
    the requirement to the index and came back with nothing to upgrade; the
    version its hint names is the version this installation is running; and an
    independent resolution that ignores the pin agrees there is nothing newer to
    install. Only the last of those establishes currency, because the first two
    are what *every* exact-pinned installation shows whether or not a newer
    release exists: `uv tool upgrade` resolves under the pin, so its `Nothing to
    upgrade` is the pin agreeing with itself, and the pin naming the installed
    version says where the installation is, not that it is the newest. Reading
    those two as proof of currency reported a stale pin -- one below a release the
    index offers -- as `already_current` and exit 0 (round 1, finding 1). The
    unpinned resolution is what tells the pin at the newest release from the pin
    below one.

    Reporting a pin at the newest release as `upgrade_blocked_by_pin` with exit 1
    was the other defect: on a machine pinned to the current release,
    `agentic-hil upgrade` refused every time and a provisioning script that runs
    it read a failure over an installation that was exactly where it should be
    (#450). So the note is kept for that case and the refusal for the one it hides
    behind.

    Anything short of the independent resolution confirming currency falls to the
    refusal: a hint whose version this cannot read, a resolution the manager could
    not be asked (pipx, or a `uv` that answered unreadably), or one that names a
    newer release. Currency this cannot establish is a pin left reported as a
    block, which is the safe direction to fall.

    Returns the currency verdict together with the resolution the unpinned query
    returned and the certificate note it earned, so the pin outcome carries what
    that query met rather than dropping it (round 1, finding 2). The two guards
    that reject a pin before the query runs return no resolution and an empty
    note: nothing was asked, so there is nothing to carry.
    """
    if _NOTHING_TO_UPGRADE not in _manager_output(install_result).lower():
        return False, None, _CertificateNote("", {})
    if _pinned_version(install_result) != current_version:
        return False, None, _CertificateNote("", {})
    currency, resolution, note = _installed_release_is_current(manager, command)
    return currency is True, resolution, note


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

    The extras are not the only thing such a line drops, which is what
    `_recorded_install` is here for. `uv tool install --with pytest` records
    pytest in the receipt beside the root requirement, and a reinstall that
    names only the root uninstalls it: measured on a pinned bench, where this
    command's own remediation removed the pytest the operator had added and
    then reported that it had kept the installation whole. So every recorded
    requirement is replayed as its own `--with`, and a recorded interpreter as
    `--python`, exactly as the one-line installer's refresh path replays them.
    """
    requirement = _upgrade_requirement()
    if manager == "pipx":
        return f'pipx install --force "{requirement}"'
    if command[1:3] == ["tool", "upgrade"]:
        recorded = _recorded_install()
        options = [f"--python {_shell_quoted(recorded.python)}"] if recorded.python else []
        options.extend(f"--with {_shell_quoted(name)}" for name in recorded.with_requirements)
        return f'uv tool install {"".join(f"{option} " for option in options)}"{requirement}@latest"'
    return None


def _shell_quoted(value: str) -> str:
    """One receipt value, quoted for the shell this line is meant to be pasted into.

    The values interpolated here come out of uv's receipt, and they were dropped
    into a double-quoted slot with no quoting of their own: a recorded name of
    `pytest" ; echo pwned #` rendered as
    `uv tool install --with "pytest" ; echo pwned #" "agentic-hil@latest"`. The
    receipt belongs to the operator, so this is robustness rather than a
    privilege boundary, but the line exists to be pasted and a line that is not
    the command it prints is worse than no line.

    The same host split `empty_directory_removal_command` makes, and for the same
    reason: the shell a Windows operator pastes into is PowerShell, where a
    single-quoted string is literal and a doubled quote is the escape, and
    everywhere else it is a POSIX shell, which is what `shlex.quote` is for.
    """
    if os.name == "nt":
        literal = value.replace("'", "''")
        return f"'{literal}'"
    return shlex.quote(value)


def _plain_line_would_remove(installed_extras: tuple[str, ...], recorded: _RecordedInstall) -> str:
    """What uv's own hint would take off this installation, in words, or nothing.

    The pin refusal already carried `reinstall_command` and `installed_extras`,
    and an operator still ran the bare line uv prints beside them, because
    nothing on the result said what running it costs. So the cost is stated:
    which extras, which recorded packages, which interpreter, by name. Empty
    where there is nothing to lose, so a plain installation is not handed a
    warning about a difference it does not have.
    """
    losses: list[str] = []
    if installed_extras:
        named = ", ".join(f"[{extra}]" for extra in installed_extras)
        losses.append(f"the {named} {'extra' if len(installed_extras) == 1 else 'extras'} and everything they installed")
    if recorded.with_requirements:
        losses.append(f"{', '.join(recorded.with_requirements)}, which uv's receipt records as installed alongside it")
    if recorded.python:
        losses.append(f"the interpreter {recorded.python} this installation was created with")
    if not losses:
        return ""
    return (
        "Do not run the bare `uv tool install \"agentic-hil@latest\"` that uv's own hint names: it records the "
        f"distribution alone, so it removes {_named_series(losses)}."
    )


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
# than guessed. A `pyocd` debugger is deliberately absent: the backend runs an
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
    first `can_session_start` was what found out, hours after the upgrade that
    caused it, and in a result about a bus rather than about the installation.
    `uv tool install --upgrade` *replaces* the recorded requirement rather than
    raising it, so a bench installed as `agentic-hil[can]` came back without the
    extra and nothing said so.

    Read off `_installed_extras`, which asks the installed distribution rather
    than trying an import: the question is what this installation has, and that
    is also what decides the reinstall command. The command names the union (the
    extras that are here plus the ones the configuration needs) because a
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


def _unresolved_location(path: str | Path) -> str:
    """A path in the one spelling two of them compare in, symlinks left alone.

    The sibling of `_normalized_location`, and the difference is the whole point
    of it: that one resolves, which is right for an image the kernel already
    resolved, and destroys the one fact a command line carries. A virtual
    environment's `bin/python` is a symlink to the system interpreter, so
    resolving the path a process was *invoked* by answers where that interpreter
    lives rather than which environment invoked it.
    """
    return os.path.normcase(os.path.abspath(str(path))).replace("\\", "/").casefold()


def _location_spellings(path: str) -> tuple[str, ...]:
    """One path as written and, where they differ, as it resolves.

    Both, because the two ends of the comparison are written by different
    things: `sys.prefix` may itself sit behind a symlinked home directory, and a
    command line names whatever the caller typed.
    """
    written = _unresolved_location(path)
    with suppress(OSError, ValueError):
        resolved = _normalized_location(path)
        if resolved != written:
            return (written, resolved)
    return (written,)


def _under_owned_prefix(path: str, owned_prefixes: tuple[str, ...]) -> bool:
    return any(spelling.startswith(prefix) for spelling in _location_spellings(path) for prefix in owned_prefixes)


def _owned_prefixes(owned_root: Path | None) -> tuple[str, ...]:
    """The environment this distribution owns alone, in every spelling it has."""
    if owned_root is None:
        return ()
    return tuple(dict.fromkeys(spelling.rstrip("/") + "/" for spelling in _location_spellings(str(owned_root))))


def _belongs_to_installation(entry: ProcessImage, owned_prefixes: tuple[str, ...], scripts: tuple[str, ...]) -> bool:
    """Whether one running process belongs to the installation being replaced.

    The image decides it on Windows, where a virtual environment's interpreter
    is a copy and `QueryFullProcessImageNameW` therefore answers a path inside
    the environment.

    It decides nothing on Linux, and that is the reported defect: a POSIX
    virtual environment's `bin/python` is a symlink to the system interpreter,
    so `/proc/<pid>/exe` resolves to `/usr/bin/python3.x` for every process the
    environment starts. Measured on a bench with a live, initialised
    `agentic-hil mcp-stdio` running out of the tool environment:
    `agentic-hil upgrade --json` answered `restart_required: false`, named
    nobody, and said no restart was needed. Nothing this project shipped for
    naming running servers worked on Linux at all.

    So two more ways in, both about how the process was started rather than what
    the kernel resolved it to. The path it was invoked by is argv[0] or argv[1],
    which is the console script, or the environment's own `bin/python` as the
    caller spelled it; and `VIRTUAL_ENV` is what an activated environment puts
    in its children. Either one under this installation's prefix is this
    installation's process. Both are read with the same tolerance the image gets:
    a process that answers neither is a process this cannot claim, never a
    failure.
    """
    if _under_owned_prefix(entry.image, owned_prefixes):
        return True
    if any(spelling in scripts for spelling in _location_spellings(entry.image)):
        return True
    if not owned_prefixes:
        return False
    if entry.virtual_env and any(spelling.rstrip("/") + "/" in owned_prefixes for spelling in _location_spellings(entry.virtual_env)):
        return True
    return any(_under_owned_prefix(argument, owned_prefixes) for argument in entry.launch_arguments if argument)


def _upgrading_process_and_its_launchers(by_pid: dict[int, ProcessImage], owned_prefixes: tuple[str, ...], scripts: tuple[str, ...]) -> set[int]:
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
        if not _belongs_to_installation(parent, owned_prefixes, scripts):
            break
        if parent.created_ns > current.created_ns:
            # A pid is reused once its process exits. Nothing can be younger
            # than the child it started, so this entry is an unrelated process
            # that inherited the number, not the launcher we came through.
            break
        excluded.add(parent.pid)
        current = parent
    return excluded


def _processes_holding_installation() -> list[JsonObject] | None:
    """Processes running out of the installation an upgrade is about to replace.

    Read before the manager runs and reported afterwards, because those are the
    processes the swap does not reach: a running server keeps the files it has
    mapped and goes on answering with the previous version until the host that
    started it restarts. Naming them is the whole of what `restart_required`
    owes an operator, and it is not a reason to refuse an upgrade the operator
    typed.

    None on a host whose process table this cannot read, and that is not the
    empty list's claim: macOS publishes no such table and a Windows snapshot can
    fail, and both used to arrive here as "nothing is running out of this
    installation", which put "No restart is needed." on the two already-current
    summaries of a machine that could not have known. The callers say so instead
    and leave `restart_required` off the result rather than answering it false.

    Each entry carries what it takes to find the process again: the pid, the
    image it runs, the directory it was started in, which for an MCP server is
    the project its host opened it for, and when it started. The last two are
    added only where the host answered them, so no entry carries a field that
    had to be invented.
    """
    snapshot = snapshot_process_images()
    if snapshot is None:
        return None
    owned_prefixes = _owned_prefixes(_dedicated_environment_root())
    scripts = _installation_console_scripts()
    excluded = _upgrading_process_and_its_launchers({entry.pid: entry for entry in snapshot}, owned_prefixes, scripts)
    holders = [
        {"pid": entry.pid, "image": entry.image, **_where_and_when(entry)}
        for entry in snapshot
        if entry.pid not in excluded and _belongs_to_installation(entry, owned_prefixes, scripts)
    ]
    return sorted(holders, key=lambda holder: holder["pid"])


def _where_and_when(entry: ProcessImage) -> JsonObject:
    """One process's working directory and start time, each only where it is known.

    A creation time this cannot turn into a date is left off rather than
    rendered as whatever the conversion made of it. Both halves are optional for
    the same reason: a field an operator reads as a fact about their machine has
    to have come from their machine.
    """
    located: JsonObject = {}
    directory = process_working_directory(entry.pid)
    if directory:
        located["working_directory"] = directory
    started = filetime_epoch_seconds(entry.created_ns)
    if started is not None:
        with suppress(OSError, OverflowError, ValueError):
            located["started_at"] = datetime.fromtimestamp(started, tz=timezone.utc).replace(microsecond=0).isoformat()
    return located


# ---------------------------------------------------------------------------
# The launcher on PATH, moved out of the manager's way.
#
# A `uv tool` installation is two places: the environment, and one small
# trampoline per console script in uv's bin directory, copied out of that
# environment. Upgrading replaces both, and on Windows the copy of the
# trampoline is the step that fails, because every live MCP server was started
# through it and a file mapped as a running image cannot be overwritten:
#
#     Caused by: Failed to install entrypoint
#     Caused by: failed to copy file from ...\uv\tools\agentic-hil\Scripts\agentic-hil.exe
#     to ...\.local\bin\agentic-hil.exe: Der Prozess kann nicht auf die Datei
#     zugreifen, da sie von einem anderen Prozess verwendet wird. (os error 32)
#
# Windows does not refuse to *rename* that file. A rename moves the directory
# entry and the mapping follows the file, so the running launcher keeps the
# image it has and the manager's copy lands on a name nothing holds any more.
# That is the whole of the fix: rename before the manager runs, put back
# whatever the manager did not replace after all, and delete the leftovers on a
# later run once nothing maps them.

# What a superseded launcher is renamed to. It has to be a sibling (a rename
# across volumes is a copy, which is the operation that just failed), it has to
# be a name no shell resolves, and it has to be recognisable to the sweep below
# without matching anything this installation did not write.
_SUPERSEDED_SUFFIX = ".superseded"
# How many superseded copies of one launcher may pile up before this stops
# making more. Reached only on a bench where nothing has restarted through
# several upgrades, and stopping there loses the rename rather than the upgrade.
_SUPERSEDED_LIMIT = 32


def _manager_bin_directory() -> Path | None:
    """Where the manager keeps the launchers it copies out of this environment.

    Only the two managers with an environment of their own have such a
    directory: `uv tool` and `pipx` install the package away from PATH and put
    a trampoline where PATH can see it. The pip and `uv pip` routes write their
    console scripts into the scheme's own scripts directory, which is inside
    the installation and is not a separate copy to keep in step.

    Read out of the environment rather than out of the manager, because asking
    the manager means running it, and the one question this needs answered is
    where a file is. The order is uv's own documented one, and `pipx` reads a
    single variable of its own; where neither says anything, both land on the
    same `~/.local/bin`.
    """
    manager = owning_manager()
    variables = {"uv-tool": ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME"), "pipx": ("PIPX_BIN_DIR",)}.get(manager)
    if variables is None:
        return None
    for variable in variables:
        configured = os.environ.get(variable, "").strip()
        if configured:
            return Path(configured)
    if manager == "uv-tool":
        data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        if data_home:
            return Path(data_home).parent / "bin"
    with suppress(RuntimeError, OSError):
        return Path.home() / ".local" / "bin"
    return None


def _entrypoint_names() -> tuple[str, ...]:
    """Every console script this installation's own distribution declares.

    Only `agentic-hil`'s own entry points, and deliberately not the ones the
    installed extras' distributions declare. The launchers this touches live in
    the shared bin of an isolated manager -- `_manager_bin_directory` names one
    only for `uv-tool` and `pipx` -- and neither exposes a dependency's
    executables there by default: `uv tool` installs only the requested
    package's scripts, and `pipx` exposes a dependency's only when it was
    installed with `--include-deps`. So an ordinary `agentic-hil[can]`
    installation does not own `can_logger`; a same-named file in the shared bin
    belongs to whatever put it there, and renaming or sweeping it would move an
    unrelated tool's launcher out of PATH (review round 0, finding 3). Deriving
    the extras' scripts and assuming this installation owns them was exactly that
    false inference.

    `agentic-hil` is seeded rather than looked up so that unreadable metadata
    still costs nothing but any further console scripts this package itself
    declares, never the one name this module is about.
    """
    from importlib.metadata import distribution

    names = {"agentic-hil"}
    with suppress(Exception):
        names.update(entry.name for entry in distribution("agentic-hil").entry_points if entry.group == "console_scripts")
    return tuple(sorted(names))


def _installation_launchers() -> list[Path]:
    """The launcher files that exist right now, for those names, in that directory.

    Both spellings are tried and only what is there is returned: a name is a
    file on Windows with `.exe` on it and without on POSIX, and a candidate that
    does not exist is a console script this manager never installed.
    """
    directory = _manager_bin_directory()
    if directory is None:
        return []
    found: list[Path] = []
    for name in _entrypoint_names():
        for candidate in (directory / f"{name}.exe", directory / name):
            with suppress(OSError):
                if candidate.is_file():
                    found.append(candidate)
    return found


def _superseded_name(launcher: Path) -> Path | None:
    """A free sibling name for one launcher, or None when there is no room left."""
    for index in range(_SUPERSEDED_LIMIT):
        candidate = launcher.with_name(_superseded_sibling(launcher.name, index))
        with suppress(OSError):
            if not candidate.exists():
                return candidate
    return None


def _superseded_sibling(stem: str, index: int) -> str:
    """The one sibling name index `index` maps to, the only spellings there are.

    `.superseded` for the first and `.superseded.<index>` after it: this is the
    single place the shape is written, so the generator above and the sweep below
    agree on the exact, finite set of names and nothing has to infer it from a
    glob."""
    return f"{stem}{_SUPERSEDED_SUFFIX}{f'.{index}' if index else ''}"


def _superseded_siblings(stem: str) -> tuple[str, ...]:
    """Every sibling name a superseded copy of `stem` could ever have been given."""
    return tuple(_superseded_sibling(stem, index) for index in range(_SUPERSEDED_LIMIT))


def _remove_superseded_launchers() -> None:
    """Delete what an earlier upgrade renamed aside, once nothing maps it.

    A delete that fails because a launcher of the previous release is still
    running is the ordinary state of a bench whose sessions have not restarted
    yet, so the file is left exactly where it is and nothing is reported: it is
    not on PATH under that name, it costs a few kilobytes, and the next upgrade
    asks again. Only names this module itself wrote are looked at, so a sweep
    can never reach a file somebody else put in a shared bin directory.

    "Names this module itself wrote" is meant exactly: the finite set
    `_superseded_name` can hand out, enumerated rather than matched by a glob. A
    `<launcher>.superseded*` glob also matched an operator's own
    `<launcher>.exe.superseded-backup`, which shares the prefix but is not a name
    this generator produces, and `unlink()` deleted it for good (review round 0,
    finding 2). And the sweep is confined to the hosts where the rename mechanism
    runs at all -- the ones that cannot replace a running file -- because nowhere
    else did this module ever create a superseded copy to clean up, so nowhere
    else is there anything of ours to reach.
    """
    if not _host_locks_running_files():
        return
    directory = _manager_bin_directory()
    if directory is None:
        return
    for name in _entrypoint_names():
        for stem in (f"{name}.exe", name):
            for sibling in _superseded_siblings(stem):
                leftover = directory / sibling
                with suppress(OSError):
                    if leftover.is_file():
                        leftover.unlink()


def _launchers_moved_aside() -> list[tuple[Path, Path]]:
    """Rename the launchers the manager is about to replace out of its way.

    Empty on a host that lets a running executable be replaced, which is every
    platform but Windows: there the manager unlinks the old file, the process
    running it keeps reading its own copy, and a rename would only leave a
    second file behind for nothing.

    A rename that fails is not a failure of the upgrade. It means the manager
    meets the file the way it always did, which is the behaviour this replaces,
    so the run goes on and reports whatever the manager makes of it.
    """
    if not _host_locks_running_files():
        return []
    moved: list[tuple[Path, Path]] = []
    for launcher in _installation_launchers():
        aside = _superseded_name(launcher)
        if aside is None:
            continue
        with suppress(OSError):
            launcher.rename(aside)
            moved.append((launcher, aside))
    return moved


class _RestoredLaunchers(NamedTuple):
    """What restoring the moved-aside launchers left standing.

    `superseded` are the sibling names kept because the manager did write the
    launcher, so the old image sits under a name nothing on PATH resolves.
    `unrecovered` are the launchers the manager did not write and the rename-back
    could not put back either: PATH has no launcher under that name and only the
    `.superseded` sibling beside it, so the CLI is off PATH until that sibling is
    moved back by hand. Each is named as a `(launcher, sibling)` pair so a
    result can tell an operator exactly which file to restore."""

    superseded: list[str] = []
    unrecovered: list[tuple[str, str]] = []


def _restore_unreplaced_launchers(moved: list[tuple[Path, Path]]) -> _RestoredLaunchers:
    """Put back every launcher the manager did not write after all.

    The rename is only safe because of this: a manager that failed before it
    reached the entrypoints, or one that had nothing to install, would otherwise
    leave the installation with no launcher on PATH at all. So each name is
    asked once more after the manager stops, and one that is still missing gets
    its old image back under the name that was there before.

    A rename-back that itself fails used to be swallowed, which left the CLI off
    PATH with only its `.superseded` sibling to show for it and a result that
    still called the installation intact (review round 0, finding 4). So the
    launchers that stay missing are reported, for the caller to surface as
    cleanup-required rather than to bury.
    """
    superseded: list[str] = []
    unrecovered: list[tuple[str, str]] = []
    for launcher, aside in moved:
        replaced = False
        with suppress(OSError):
            replaced = launcher.exists()
        if replaced:
            superseded.append(str(aside))
            continue
        try:
            aside.rename(launcher)
        except OSError:
            still_missing = True
            with suppress(OSError):
                still_missing = not launcher.exists()
            if still_missing:
                unrecovered.append((str(launcher), str(aside)))
    return _RestoredLaunchers(superseded, unrecovered)


def _with_unrecovered_launchers(outcome: JsonObject, restore: _RestoredLaunchers) -> JsonObject:
    """Fail an outcome that left the CLI launcher off PATH, naming the sibling.

    A launcher the manager did not write and the rename-back could not restore is
    a broken installation whatever the version probe answered: the module still
    imports, but the console script an operator types is gone and only its
    `.superseded` sibling remains. So this outcome is failed and cleanup-required
    even over an otherwise clean upgrade, and it names each file to move back
    (review round 0, finding 4). A no-op when every launcher was restored, which
    is every ordinary run."""
    if not restore.unrecovered:
        return outcome
    off_path = ", ".join(launcher for launcher, _ in restore.unrecovered)
    summary = str(outcome.get("summary") or "").rstrip()
    note = (
        f"The CLI launcher could not be put back after the upgrade: {off_path} is off PATH and only its "
        "`.superseded` sibling remains, so the console script will not run. Rename each `recover_from` file back to "
        "its `launcher` name before calling the CLI again."
    )
    return {
        **outcome,
        "ok": False,
        "installation_intact": False,
        "cleanup_required": True,
        "restart_required": False,
        "unrecovered_launchers": [{"launcher": launcher, "recover_from": sibling} for launcher, sibling in restore.unrecovered],
        "summary": f"{summary} {note}" if summary else note,
    }


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

# The values that turn UV_SYSTEM_CERTS off. An operator who exported one of these
# has deliberately kept uv on its own bundled roots, which is an opt-out to
# preserve rather than proof uv read this machine's store: setting the variable
# again would leave it disabled, and overriding it would silently reverse a
# choice they made. Any other non-empty value is read as enabling the store.
_UV_SYSTEM_CERTS_DISABLED_VALUES = frozenset({"0", "false", "no", "off", "n", "f"})

# The one thing that fixes a proxy neither the manager's roots nor this machine's
# store trusts, taken from the catalogue rather than written again here. It is
# standing text, so `upgrade_failed` states it as a catalogue step on every
# refusal of that type; attaching it as a step as well is what keeps the outcomes
# with a catalogue entry of their own -- `installation_broken` and
# `installation_changed_after_failed_upgrade` -- from losing it. Where both would
# say it, `_with_certificate_note` drops the copy.
_INSTALL_THE_PROXY_CA = INSTALL_THE_PROXY_CA


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


def _uv_system_certs_disabled(value: str) -> bool:
    """Whether an exported UV_SYSTEM_CERTS explicitly turns the system store off."""
    return value.strip().lower() in _UV_SYSTEM_CERTS_DISABLED_VALUES


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
    bundle they chose for one this module picked. Two of those inherited values
    do not name the system store, though, and the remedy has to say so: a
    `UV_SYSTEM_CERTS` set to a disabled value kept uv on its own roots, so the
    fix is to enable it rather than to install a CA into a store uv is not
    reading; and a `PIP_CERT` names one specific bundle, so the CA has to go into
    that bundle rather than only into the machine store pip was told to ignore.
    """
    if manager == "uv":
        configured = os.environ.get("UV_SYSTEM_CERTS", "").strip()
        if configured and _uv_system_certs_disabled(configured):
            return _SystemStoreAttempt(
                no_attempt=(
                    f"UV_SYSTEM_CERTS is set to {configured!r} here, which keeps uv on its own bundled roots, so uv did "
                    f"not read this machine's store on the attempt that failed. That opt-out is yours to hold, so it was "
                    f"not overridden and no second attempt was made against it."
                ),
                no_attempt_brief=f"not retried, because UV_SYSTEM_CERTS is set to the disabled value {configured!r} here, an opt-out this leaves in place",
                next_step=(
                    "To let uv read this machine's own certificate store, set UV_SYSTEM_CERTS to 1 (or unset it) and "
                    "make sure the proxy's own CA certificate is installed in that store; until uv is reading the store, "
                    "installing the CA there cannot help, and no switch that turns verification off is a fallback. "
                    "TROUBLESHOOTING.md section 1 is the rest of it."
                ),
            )
        if configured:
            return _SystemStoreAttempt(
                no_attempt=(
                    "UV_SYSTEM_CERTS is already set in this environment, so uv read this machine's own store on the "
                    "attempt that just failed and a second one would only repeat it."
                ),
                no_attempt_brief="not retried, because UV_SYSTEM_CERTS is already set here and was in force for the attempt that failed",
                next_step=_INSTALL_THE_PROXY_CA,
            )
        return _SystemStoreAttempt("UV_SYSTEM_CERTS", "1")
    pip_cert = os.environ.get("PIP_CERT", "").strip()
    if pip_cert:
        return _SystemStoreAttempt(
            no_attempt=(
                f"PIP_CERT is already set in this environment, so pip read the bundle it names ({pip_cert}) on the "
                f"attempt that just failed and a second one would only repeat it."
            ),
            no_attempt_brief="not retried, because PIP_CERT is already set here and was in force for the attempt that failed",
            next_step=(
                f"Add the proxy's own CA certificate to the bundle PIP_CERT names ({pip_cert}) -- that is the only "
                f"certificate source pip read on the attempt that failed, so installing a CA anywhere else does not "
                f"reach it -- or point PIP_CERT at a bundle that already trusts the proxy (unsetting it falls back to "
                f"pip's default) and run the upgrade again. No switch that turns verification off is a fallback. "
                f"TROUBLESHOOTING.md section 1 is the rest of it."
            ),
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

    Both invocations an upgrade makes come through here, and the same retry is
    the point of that: the mutating install, and the `--dry-run` question asked
    before it. They reach the same index over the same network, so a proxy that
    stops one stops the other, and a retry only the install had would leave the
    guard answering "cannot tell" and falling through to the install it exists to
    prevent.

    Three values: the attempt whose outcome the caller reports, that attempt's
    captured output, and the certificate fields the result carries, which are
    empty whenever the manager said nothing about a certificate at all. Where the
    output is recorded is the caller's: `install` for the upgrade itself,
    `resolution` for the question.

    The retried attempt is the one reported, and the attempt it replaces is kept
    under that payload's `certificate_retry.first_attempt` rather than dropped. A
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
    # The second attempt failed too, but not necessarily for the reason the first
    # did. Only a retry whose own words name a trust failure again says the
    # machine's store does not carry the proxy CA either; a retry that failed
    # while resolving, downloading or installing got *past* the trust failure and
    # then fell over somewhere else, and telling that operator to install a CA
    # sends them at the wrong thing. So the retry is classified on its own output
    # rather than assumed to have ended the way the first attempt did.
    if _trust_failure(_manager_output(install_result)):
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
    return (
        retried,
        install_result,
        _certificate_note(
            f"{diagnosis} {retried_with}, and that got the manager past the trust failure: the second attempt names no "
            f"untrusted certificate, so this machine's store was read, but it then failed for a reason of its own. That "
            f"reason is the manager's to report and is preserved under install; it is not a certificate to install, so "
            f"do not change the trust store on account of it.",
            f"{retried_briefly}, which got past the trust failure but then failed for a different reason the manager records under install",
            [_persistent_export(attempt)],
        ),
    )


def _with_certificate_note(outcome: JsonObject, note: _CertificateNote) -> JsonObject:
    """The outcome, plus what happened about certificates, wherever it ended up.

    One place rather than five. A trust failure is answered the same way whether
    the retry then upgraded the machine, found nothing left to do, failed again
    or left an installation that will not load, and what happened belongs in the
    summary on every one of them, because that is the line a person reads first.

    The measured `next_steps` are where the proxy CA imperative lives, and they
    are attached only on a run that met a proxy, so a failure that met none never
    reads it: the `upgrade_failed` catalogue names the CA solely through its
    conditional `certificates` clause and states no bare imperative anywhere. A
    step an outcome's own remediation *does* already carry is still dropped here
    rather than attached twice, so a sentence printed under What to do is never
    repeated under Next steps on one screen; the guard costs nothing when there is
    no overlap and keeps the note honest should a future entry grow one.
    """
    if not note.said:
        return outcome
    summary = str(outcome.get("summary", "")).strip()
    standing = {step for step in outcome.get("remediation") or [] if isinstance(step, str)}
    fields = dict(note.fields)
    steps = [step for step in fields.get("next_steps") or [] if step not in standing]
    if steps:
        fields["next_steps"] = steps
    else:
        fields.pop("next_steps", None)
    return {**outcome, **fields, "summary": f"{summary} {note.said}".strip()}


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


def _would_install_nothing(manager: str, command: list[str]) -> tuple[bool, JsonObject | None, _CertificateNote]:
    """Whether this upgrade would install nothing, decided without installing anything.

    The triple is (answer, what the manager said, what happened about
    certificates). A False answer means either that there is something to
    install or that the question could not be put to this manager at all, and
    the two are deliberately not distinguished: both lead to the same place,
    which is running the upgrade as before.

    Asked through `_manager_run`, which is what gives this question the same
    one-shot certificate retry the install has. Behind a TLS-intercepting proxy
    every request to the index fails alike, and until this shared the retry the
    guard was the one that failed first: the query came back unreadable, "cannot
    tell" fell through to the mutating install, that install met the same proxy,
    and #326's retry then made it succeed. The end state was right and the guard
    had silently not guarded -- a machine that needed nothing had been
    uninstalled and reinstalled to find that out. Sharing the runner rather than
    copying it is also what keeps the signature list, the child-environment
    variables and the rule that an operator's own exported variable is never
    overridden identical on both, which is the only way the two can stay
    answers to the same question about the same network.
    """
    query = _resolution_query(manager, command)
    if query is None:
        return False, None, _CertificateNote("", {})
    try:
        answered, resolution, certificates = _manager_run(manager, query)
    except (OSError, subprocess.TimeoutExpired):
        return False, None, _CertificateNote("", {})
    if answered.returncode != 0:
        return False, resolution, certificates
    if manager == "pip":
        try:
            report = json.loads(answered.stdout)
        except (json.JSONDecodeError, TypeError):
            return False, resolution, certificates
        planned = report.get("install") if isinstance(report, dict) else None
        return isinstance(planned, list) and not planned, resolution, certificates
    return _UV_PIP_NO_CHANGES in _manager_output(resolution).lower(), resolution, certificates


def _unpinned_resolution_query(manager: str, command: list[str]) -> list[str] | None:
    """An index resolution that ignores the recorded pin, or None where there is none.

    The pin branch needs the one fact its own manager will not give it: whether
    the release this installation is pinned to is the newest the index offers. A
    `uv tool upgrade` reads the requirement it recorded and resolves under the
    pin, so `Nothing to upgrade` from it is the pin agreeing with itself and says
    nothing about a newer release. The question has to be put without the pin, and
    for a `uv`-managed installation `uv pip` against this very interpreter is what
    puts it, and `--dry-run` keeps it from touching the environment. The
    interpreter is `sys.executable`, which for a `uv tool` installation is the
    tool environment's own Python, so the resolution is asked about the very
    environment the pin holds.

    The upgrade is scoped to the one distribution the question is about, with
    `--upgrade-package agentic-hil` rather than a global `--upgrade`. uv's global
    `--upgrade` re-resolves the *whole* dependency set to the newest each allows,
    so an installation already at the newest Agentic HIL release but carrying one
    older-yet-compatible dependency had the dry run propose that dependency's
    update, come back without `Would make no changes`, and be read as a pin below
    a newer release -- refused as `upgrade_blocked_by_pin` over an Agentic HIL that
    was exactly current (round 1, finding 1). Scoping the upgrade to `agentic-hil`
    asks uv only whether *this* distribution has a newer release, leaves a stale
    dependency where it is, and a newer Agentic HIL that needs a newer dependency
    still moves both, which is a real upgrade and reads as one.

    None for pipx, whose upgrade path can also carry a pin but which publishes no
    resolver this can drive without risking the environment it is asking about; a
    pin it cannot get an independent answer for stays reported as a block, which
    is the safe direction to fall.
    """
    if manager == "uv" and command[1:3] == ["tool", "upgrade"]:
        return [command[0], "pip", "install", "--python", sys.executable, "--upgrade-package", "agentic-hil", "--dry-run", _upgrade_requirement()]
    return None


def _installed_release_is_current(manager: str, command: list[str]) -> tuple[bool | None, JsonObject | None, _CertificateNote]:
    """Whether an unpinned resolution would install nothing over what is here.

    Three values: the currency decision, the resolution the manager returned, and
    the certificate note that resolution earned. The decision is True when the
    newest release the index offers is the one already installed, False when it
    would install a newer one, and None when the question could not be put to this
    manager or its answer could not be read. Only True turns a pin refusal into
    the note that the pin holds nothing back; both False and None leave the pin
    reported as the block it may be.

    The resolution and the note travel with the decision rather than being
    discarded, so the pin outcome the caller builds can carry what this query met
    (round 1, finding 2). Behind a TLS-intercepting proxy the query fails the way
    every index request does; if the one-shot certificate retry then establishes
    currency, the `already_current` note it produces is what says so, and if it
    does not, the same evidence rides on the `upgrade_blocked_by_pin` result
    rather than being hidden behind it. A query that never ran -- the wrong
    manager, an `OSError`, a timeout -- has no resolution and an empty note.

    Asked through `_manager_run`, so it meets the same one-shot certificate retry
    the install and the pre-flight resolution do: a question that fails the way
    every index request does comes back unreadable and falls to None -- the safe
    direction -- rather than being read as a currency it could not establish.
    `_UV_PIP_NO_CHANGES` is the prose `uv pip install --dry-run` prints when the
    environment already satisfies the newest requirement; a wording that stops
    matching reads as False, which keeps the pin reported as a block.
    """
    query = _unpinned_resolution_query(manager, command)
    if query is None:
        return None, None, _CertificateNote("", {})
    try:
        answered, resolution, certificates = _manager_run(manager, query)
    except (OSError, subprocess.TimeoutExpired):
        return None, None, _CertificateNote("", {})
    if answered.returncode != 0:
        return None, resolution, certificates
    return _UV_PIP_NO_CHANGES in _manager_output(resolution).lower(), resolution, certificates


# ---------------------------------------------------------------------------
# Whether "nothing to install" also means "there is nothing newer".
#
# The managers answer the first question and neither of them answers the
# second. `uv tool upgrade` resolves the requirement it recorded, under the
# options it recorded, and prints `Nothing to upgrade` when that resolution
# moves nothing: a receipt carrying `exclude-newer` makes it print exactly that
# sentence two hours after a release, and every day after. Reading it as
# `already_current` put that claim on a bench that was a release behind, with a
# follow-up sentence pointing at the recorded *requirement*, which was unpinned;
# the recorded option was what held it. uv offers no command that clears such an
# option, so the operator was left with a wrong claim and no next step.
#
# The index is the only thing that knows what the newest release is, so it is
# asked, once, over HTTPS, with a short timeout, and an index that cannot be
# reached takes the claim away instead of making it. Nothing here refuses an
# upgrade or changes what a manager was told to do: the answer decides only what
# the result is allowed to say about it.

_RELEASE_INDEX_URL = "https://pypi.org/pypi/agentic-hil/json"
# Short, because this runs after the work is done and its answer only shapes a
# sentence: a bench on a slow link gets the unchecked wording rather than a
# command that hangs.
_RELEASE_INDEX_TIMEOUT_S = 5.0
# The whole request's budget, against a monotonic clock, because the timeout
# above is a socket timeout and not a wall clock: it bounds how long one read
# may wait for the next byte, and an endpoint that sends one byte every second
# resets it every second. So `agentic-hil upgrade` and the `server_upgrade` MCP
# tool could sit here far past five seconds against a slow or hostile endpoint,
# for a sentence. The read is bounded by this instead, and a deadline that
# passes is the same answer as an index that said nothing.
_RELEASE_INDEX_DEADLINE_S = 10.0
# How much is asked for per read. Small enough that the deadline is checked
# often on a trickling connection, large enough that the real payload arrives in
# a few passes.
_RELEASE_INDEX_CHUNK_BYTES = 65_536
# A payload larger than this is not an answer this reads; it is truncated and
# fails to parse, which lands on the same "could not be checked" wording.
_RELEASE_INDEX_MAX_BYTES = 8_000_000
# The recorded options that make a resolution pass over a release that exists.
# `exclude-newer` is recorded whether it was given as a flag or through the
# environment, and both spellings uv writes are read.
_RESOLUTION_HOLDING_OPTIONS = ("exclude-newer", "exclude_newer", "exclude-newer-package", "exclude_newer_package")
# The two shapes this project publishes: `X.Y.Z` and the `X.Y.Z.devN` that
# anticipates it.
_RELEASE_NUMBER = re.compile(r"^(?P<release>\d+\.\d+\.\d+)(?:\.dev(?P<serial>\d+))?$")


def _newest_released_version() -> str | None:
    """What the index publishes as the newest release, or None when it did not answer.

    One request, and every failure is the same answer: a bench behind a proxy,
    an air-gapped machine, an index whose payload changed shape and one that
    answers too slowly all mean that this result may not say what the newest
    release is. The seam a test replaces is this function, so no test of the
    upgrade reaches the network.

    The deadline is taken before the request rather than after the connection,
    because the connection is part of what can be slow.
    """
    request = Request(_RELEASE_INDEX_URL, headers={"Accept": "application/json", "User-Agent": f"agentic-hil/{__version__}"})
    deadline = monotonic() + _RELEASE_INDEX_DEADLINE_S
    try:
        with urlopen(request, timeout=_RELEASE_INDEX_TIMEOUT_S) as response:
            body = _index_payload_within(response, deadline)
        if body is None:
            return None
        payload = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, HTTPException):
        return None
    info = payload.get("info") if isinstance(payload, dict) else None
    newest = info.get("version") if isinstance(info, dict) else None
    return newest.strip() if isinstance(newest, str) and newest.strip() else None


def _index_payload_within(response: object, deadline: float) -> bytes | None:
    """The response body, in bounded reads, or None once the deadline has passed.

    `urlopen(..., timeout=)` is a socket timeout: it bounds the wait for the
    next byte and starts again with every byte that arrives. A single
    `response.read(limit)` therefore loops inside the socket until the limit or
    the end of the stream, so an endpoint trickling a byte at a time held
    `agentic-hil upgrade` and the `server_upgrade` MCP tool for as long as it
    liked, against a comment promising five seconds.

    Reading a chunk at a time against a monotonic clock is what bounds it: the
    deadline is checked before every read, and one that has passed is reported
    the way an unreachable index is, because that is what the caller does with
    both. A partial body is thrown away rather than parsed, since half a payload
    is not a smaller answer.
    """
    read = getattr(response, "read", None)
    if not callable(read):
        return None
    chunks: list[bytes] = []
    remaining = _RELEASE_INDEX_MAX_BYTES
    while remaining > 0:
        if monotonic() >= deadline:
            return None
        chunk = read(min(_RELEASE_INDEX_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _release_ordering_key(version: str) -> tuple[int, int, int, int, int] | None:
    """PEP 440 ordering for the two shapes this project publishes, or None for any other.

    `1.2.3 < 1.2.4.dev0 < 1.2.4`, which is the ordering every resolver reads and
    the one `tools/check_version_consistency.py` sorts this project's own
    versions by. A number in any other shape is not ordered at all, and that
    lands on the unchecked wording: a claim about which of two releases is newer
    is worth nothing if it was guessed.
    """
    match = _RELEASE_NUMBER.match(version.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group("release").split("."))
    serial = match.group("serial")
    return (major, minor, patch, 0, int(serial)) if serial is not None else (major, minor, patch, 1, 0)


class _NewestRelease(NamedTuple):
    """What the index publishes, and what this installation's receipt says about it.

    `version` is empty when the index could not be read or answered something
    this cannot order, and that is the one state where `already_current` may not
    be claimed in either direction. `behind` says the index publishes a release
    newer than the one installed, and `ahead` says the installation is above
    everything the index publishes, which is a development build or a wheel that
    never went to this index; both false together is the one case where the two
    numbers are equal. `holds` names what the receipt records that keeps a
    resolution here, and `clearing_command` is the line that records the
    installation again without it, both empty when nothing recorded explains it.
    """

    version: str
    behind: bool
    ahead: bool
    holds: tuple[str, ...]
    clearing_command: str


def _resolution_holds(recorded: _RecordedInstall) -> tuple[str, ...]:
    """What this installation's own receipt records that keeps a resolution where it is.

    Read off the receipt rather than out of a manager's prose, which is what
    catches the option uv mentions nowhere: a recorded `exclude-newer` makes
    every later resolution ignore anything published since, and there is no
    `uv tool` command that clears it. The only line that does is a fresh
    `uv tool install`, which records the installation again from what it is
    given.

    The requirement counts only when it is exact. A recorded floor moves with
    the index by design, so naming one here made the refusal say that an
    installation's own `>=` was keeping it off a release, which is the opposite
    of what a floor does; such a receipt now falls to the branch that says
    nothing recorded explains it.
    """
    holds = [f"the recorded requirement `agentic-hil{recorded.exact_pin}`"] if recorded.exact_pin else []
    holds.extend(f"the recorded option `{key} = {recorded.options[key]}`" for key in _RESOLUTION_HOLDING_OPTIONS if recorded.options.get(key) not in (None, False))
    return tuple(holds)


def _newest_release(manager: str, command: list[str], current_version: str) -> _NewestRelease:
    """The index's answer about this distribution, and what a receipt makes of it."""
    newest = _newest_released_version()
    installed_key = _release_ordering_key(current_version)
    newest_key = _release_ordering_key(newest) if newest is not None else None
    if newest is None or newest_key is None or installed_key is None:
        return _NewestRelease("", False, False, (), "")
    if newest_key <= installed_key:
        return _NewestRelease(newest, False, newest_key < installed_key, (), "")
    holds = _resolution_holds(_recorded_install())
    clearing = (_unpinned_reinstall_command(manager, command) or "") if holds else ""
    return _NewestRelease(newest, True, False, holds, clearing)


def _index_agrees_sentence(check: _NewestRelease, current_version: str) -> str:
    """What the index publishes, against an installation that is not behind it.

    Two states were written as one. "and this installation is on it" was
    hard-coded in the not-behind branch and is true only where the two numbers
    are equal; measured on a bench carrying a development build against an index
    still on the release below it, it said the installation was on a release it
    was a build ahead of. A build above the index is named as what it is, and the
    third state, an index that publishes something newer, is the held-back branch
    below.
    """
    if not check.ahead:
        return f"{check.version} is the newest release the index publishes, and this installation is on it."
    development = _RELEASE_NUMBER.match(current_version.strip())
    what = (
        "which is a development build of the release that follows it"
        if development is not None and development.group("serial") is not None
        else "which is not a release this index publishes"
    )
    return f"{check.version} is the newest release the index publishes, and this installation is ahead of it at {current_version}, {what}."


def _judged_against_the_index(outcome: JsonObject, check: _NewestRelease, manager: str, current_version: str) -> JsonObject:
    """The already-current claim, kept, withdrawn or answered, by what the index said.

    Four ends, and `already_current` survives exactly one of them. The claim is
    a statement about the whole index and the manager only ever spoke about this
    installation's own recorded resolution, so it is made here or not at all.

    The two unpinned outcomes come through here, and only the last end refuses:
    an installation whose own receipt records something that demonstrably keeps
    it below a release the index publishes is the one case where the operator
    asked for a release and did not get it. The pinned path composes its own
    summary rather than having this one appended to it, because the sentences it
    would gain are sentences it already carries: the pin note printed
    `reinstall_command`'s account and the do-not-run warning twice, and did it
    under a `Refused:` heading with exit 1 over an outcome that withheld nothing.
    """
    summary = str(outcome.get("summary", "")).rstrip()
    if not check.version:
        return {
            **{key: value for key, value in outcome.items() if key != "already_current"},
            "summary": (
                f"{summary} The newest release could not be checked: the index did not answer, so whether "
                f"{current_version} is the newest release is not something this result can say."
            ),
        }
    if not check.behind:
        return {**outcome, "newest_release": check.version, "summary": f"{summary} {_index_agrees_sentence(check, current_version)}"}
    installed_extras, recorded, shape = _installation_shape()
    if not check.holds:
        return {
            **{key: value for key, value in outcome.items() if key != "already_current"},
            "newest_release": check.version,
            **shape,
            "summary": (
                f"{summary} The index publishes {check.version}, which is newer, and {manager} still resolves nothing "
                f"for this installation, so this is not the newest release. What a resolution reaches is the "
                f"installation's own: the index it is pointed at, and whether {check.version} accepts this interpreter."
            ),
        }
    named = _named_series(list(check.holds))
    return {
        **{key: value for key, value in outcome.items() if key != "already_current"},
        "ok": False,
        "error_type": "upgrade_blocked_by_recorded_option",
        "newest_release": check.version,
        "held_back_by": list(check.holds),
        **shape,
        "reinstall_command": check.clearing_command,
        "summary": _sentences(
            summary,
            f"The index publishes {check.version}, and this installation stays at {current_version} because its own "
            f"receipt records {named}. {manager} reports that as nothing to do and offers no command that clears it. "
            f"`reinstall_command` is the line that records this installation again without it, with "
            f"{_carried_by_the_reinstall(recorded)}; running it is the operator's decision.",
            _not_carried_by_the_reinstall(recorded),
            _plain_line_would_remove(installed_extras, recorded),
        ),
        **remediation_fields("upgrade_blocked_by_recorded_option"),
    }


def _restart_sentence(waiting: JsonObject) -> str:
    """The one sentence about restarting that this host is entitled to.

    Three of them, and only two existed: a notice naming the processes still
    running, the plain "No restart is needed." for a table that was read and held
    none, and, for a host whose table could not be read at all, the sentence that
    says so. macOS took the second one, which is a claim about that operator's
    machine made without looking at it.
    """
    notice = waiting.get("restart_notice")
    return str(notice) if isinstance(notice, str) and notice else "No restart is needed."


def _nothing_to_install(
    tool: str,
    manager: str,
    command: list[str],
    current_version: str,
    resolution: JsonObject | None,
    still_running: list[JsonObject] | None,
) -> JsonObject:
    """Nothing to install, answered before the installation could be put at risk.

    The same `already_current` a manager that ran and moved nothing produces, so
    a caller reads one field for one fact, plus `install_skipped` for the part
    that is new: no command capable of replacing this installation was run. Its
    absence on the other outcomes is the signal that one was.

    What the manager answered is about this installation's own recorded
    resolution and nothing more, so the claim that this is the newest release
    there is goes through `_judged_against_the_index`, which is the only thing
    here that has asked.
    """
    waiting = _nothing_new_to_load(still_running)
    return _judged_against_the_index(
        {
            "ok": True,
            "tool": tool,
            "already_current": True,
            "summary": (
                f"Agentic HIL is at {current_version} and {manager} resolves nothing to install for this installation, "
                f"so nothing was installed and no command that could replace it was run."
            )
            + f" {_restart_sentence(waiting)}",
            "manager": manager,
            "command": command,
            "install_skipped": True,
            "python": sys.executable,
            "previous_version": current_version,
            "version": current_version,
            **({"resolution": resolution} if resolution is not None else {}),
            **waiting,
        },
        _newest_release(manager, command, current_version),
        manager,
        current_version,
    )


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
            # The standing text, from the same catalogue its two siblings below
            # read: what the manager's own words are worth, what a `certificates`
            # clause means, and the one-line installer as the repair path. It is
            # on the document and not only in the rendering, because the MCP
            # caller reads the document and acts out of `remediation`.
            **remediation_fields("upgrade_failed"),
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
    two numbers are named as they are (the release still running in this
    process, and the one the half-finished run left on disk), and the repair is
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
    still_running: list[JsonObject] | None,
) -> JsonObject:
    """The outcomes a package manager reports with the same exit code as success.

    `uv tool upgrade` returns 0 for "upgraded" and for "nothing to do" alike, so
    the return code cannot tell them apart and the version can: this is reached
    only when the installation still runs the version it ran before. No answer
    here claims an upgrade -- the reported defect was the opposite pair, which
    sent an operator to a restart that reloaded the same release and left them
    believing they had moved. A restart is still asked for where servers are
    running out of this installation, because this call replaced nothing and
    what each of them imported is whatever was on disk when it started.

    Which outcome it is matters, because the next step differs: an installation
    that is already current needs nothing but that restart for the servers which
    predate the last one, and one held at a pin below a release it could be
    running needs a reinstall the operator has to decide on and run. An
    installation pinned to the release it is already on is the first of those
    and reads like the second, so it is told apart from both: exit 0, and the pin
    reported as the note it is.
    """
    base: JsonObject = {
        # Set per branch below: an installation that is already current is a
        # success (nothing to do and nothing wrong), while one held at a pin below
        # a release it could be running is not, because the operator wanted that
        # release and did not get it. Refusing every pin made `agentic-hil
        # upgrade` exit non-zero on a machine that was exactly current, which
        # breaks the provisioning scripts that run it unconditionally. The defect
        # was claiming an upgrade that never happened, not reporting that there
        # was none to make.
        "ok": False,
        "tool": tool,
        "manager": manager,
        "command": command,
        "python": sys.executable,
        "previous_version": previous_version,
        "version": current_version,
        "install": install_result,
    }
    reinstall_command = _unpinned_reinstall_command(manager, command)
    # Read once for both outcomes that call this installation current: the pinned
    # note below and the plain one at the end. Nothing was replaced on either
    # path, so a server that was already up is running whatever was on disk when
    # it started, and that is the case a `restart_required: false` hid.
    waiting = _nothing_new_to_load(still_running)
    pinned = reinstall_command is not None and _manager_reports_exact_pin(install_result)
    # The unpinned currency query runs at most once, and both pin branches below
    # read it: whether the pin holds anything back, the resolution it returned,
    # and the certificate note it earned. The resolution and note are carried onto
    # the outcome rather than dropped, so a query that met a proxy -- or failed to
    # get past one -- is on the result the operator reads (round 1, finding 2). A
    # non-pinned outcome never asks the query, so the note stays empty there.
    holds_nothing_back, currency_resolution, currency_note = (
        _pin_holds_nothing_back(install_result, current_version, manager, command) if pinned else (False, None, _CertificateNote("", {}))
    )
    currency_fields: JsonObject = {"resolution": currency_resolution} if currency_resolution is not None else {}
    if pinned:
        # What the line that clears the pin has to rebuild, read once for both pin
        # outcomes below: both print that same line, and both say in words that it
        # keeps this installation whole, so both owe the reader the same account of
        # what "whole" covers. A refusal that named only the extras is what sent an
        # operator to uv's bare hint and cost them their `--with` packages, and the
        # note branch prints the identical line for the identical purpose.
        installed_extras, recorded, shape = _installation_shape()
        loss = _plain_line_would_remove(installed_extras, recorded)
        # The index is asked on the pinned path too, and it decides nothing here.
        # Whether the pin holds a release back is settled by the unpinned
        # `--dry-run` resolution above, which put the question to the index this
        # installation is actually pointed at, through the environment the pin
        # holds; this query reads one public index over HTTPS. So the release it
        # names rides along as `newest_release`, and the one thing it can do on
        # its own is take a currency claim away, never make one.
        check = _newest_release(manager, command, current_version)
        newest_release: JsonObject = {"newest_release": check.version} if check.version else {}
        if not holds_nothing_back:
            return _with_certificate_note(
                {
                    **base,
                    "error_type": "upgrade_blocked_by_pin",
                    "summary": _sentences(
                        f"Agentic HIL was not upgraded: {manager} holds this installation at an exact version pin, so it is "
                        f"still {current_version}. Nothing was changed.",
                        _restart_sentence(waiting),
                        f"`reinstall_command` is the line that clears the pin and rebuilds this installation as it "
                        f"stands, with {_carried_by_the_reinstall(recorded)}.",
                        _not_carried_by_the_reinstall(recorded),
                        loss,
                        "Running it is the operator's decision.",
                    ),
                    "pinned_version": _pinned_version(install_result) or current_version,
                    # This outcome replaced nothing, exactly like the note below
                    # and the plain already-current answer, so a server started
                    # before it is running whatever was on disk then and is named
                    # here for the same reason it is named there. It was the one
                    # unchanged outcome that never spread these, and it reported
                    # `restart_required: false` over a live holder.
                    **waiting,
                    **shape,
                    "reinstall_command": reinstall_command,
                    "manager_hint_note": _relayed_hint_note(manager, reinstall_command),
                    **newest_release,
                    **currency_fields,
                    **remediation_fields("upgrade_blocked_by_pin"),
                },
                currency_note,
            )
        # Current, and pinned to the release it is current at. Everything the
        # refusal above carried is carried here too, because an operator who
        # wants the next release picked up automatically still has to run that
        # line; what is not carried is the error type and the exit code, which
        # would say a wanted release was withheld when none was.
        #
        # The summary is composed from parts rather than appended to, and that is
        # the whole of what went wrong before: the index judgement was run over a
        # finished sentence, so the note printed the account of `reinstall_command`
        # twice and the do-not-run warning twice, and it printed them under a
        # `Refused:` heading with exit 1 while still saying "reported here as a
        # note rather than as a refusal. No restart is needed." Each sentence is
        # named once here, and which of the index sentences it gets is the only
        # thing the index decides.
        withdrawn = check.behind
        sentences = [
            f"Agentic HIL is at {current_version}, which is the version this installation is pinned to, and "
            f"{manager} resolved nothing newer to install.",
            "The pin holds no release back while it names the one that is installed, so it is reported here as a "
            "note rather than as a refusal.",
        ]
        if withdrawn:
            # The two answers disagree: the unpinned resolution moved nothing in
            # this environment and the index publishes a release above the one
            # installed. Whatever explains it -- a recorded `exclude-newer`, an
            # index of the installation's own that has not got the release yet --
            # this is not the newest release there is, so the note keeps `ok` and
            # its exit code, because nothing was withheld from a run that asked for
            # it, and loses the currency claim, because the release that is out
            # there is newer than the one here. `held_back_by` names what the
            # receipt records. An installation is never called current while
            # either check says otherwise.
            sentences.append(
                (
                    f"The index publishes {check.version}, which is newer, so this is not the newest release there is: "
                    f"this installation stays at {current_version} because its own receipt records "
                    f"{_named_series(list(check.holds))}, which {manager} reports as nothing to do and offers no command "
                    f"that clears."
                )
                if check.holds
                else (
                    f"The index publishes {check.version}, which is newer, and {manager} still resolves nothing for "
                    f"this installation, so this is not the newest release. What a resolution reaches is the "
                    f"installation's own: the index it is pointed at, and whether {check.version} accepts this "
                    f"interpreter."
                )
            )
        elif check.version:
            sentences.append(_index_agrees_sentence(check, current_version))
        sentences.append(_restart_sentence(waiting))
        sentences.append(
            f"`reinstall_command` is the line that clears the pin and rebuilds this installation as it stands, with "
            f"{_carried_by_the_reinstall(recorded)}, for whenever later releases are to be picked up without it."
        )
        sentences.append(_not_carried_by_the_reinstall(recorded))
        sentences.append(loss)
        sentences.append("Running it is the operator's decision.")
        return _with_certificate_note(
            {
                **base,
                "ok": True,
                "summary": _sentences(*sentences),
                **({} if withdrawn else {"already_current": True}),
                **waiting,
                # What the manager's hint named, which the guard above has just
                # established is this version. Read off the hint rather than copied
                # from `version`, so a wording this stops being able to read shows up
                # as the refusal it falls to and never as a number invented here.
                "pinned_version": _pinned_version(install_result),
                **shape,
                "reinstall_command": reinstall_command,
                **newest_release,
                **({"held_back_by": list(check.holds)} if withdrawn and check.holds else {}),
                **currency_fields,
            },
            currency_note,
        )
    return _judged_against_the_index(
        {
            **base,
            "ok": True,
            "summary": f"Agentic HIL is at {current_version}; {manager} had nothing to replace. {_restart_sentence(waiting)}",
            "already_current": True,
            **waiting,
        },
        _newest_release(manager, command, current_version),
        manager,
        current_version,
    )


def _relayed_hint_note(manager: str, reinstall_command: str) -> str:
    """Whose words the captured output is, said before the operator reads them.

    A refusal prints the manager's own output, which is right: the operator
    reads their own machine's words. What it printed alongside was uv's hint to
    `reinstall with uv tool install agentic-hil@latest`, unlabelled and
    indistinguishable from this program's own advice, and then a Do-not bullet
    further down contradicting it. So the block is introduced: whose text it is,
    what following it costs, and which line on this result is the one to run.
    """
    return (
        f"The `install` block under Details below carries {manager}'s own output, printed as {manager} wrote it and "
        f"not as advice from here. Its own `reinstall with` line names the bare distribution, which is the one thing "
        f"this refusal exists to talk an operator out of: it drops the extras and any package the receipt records "
        f"beside them. The line to run is `reinstall_command`: {reinstall_command}"
    )


# What a host that publishes no process table owes a result instead of a count.
# macOS is one, and a Windows snapshot that raised is another; both are
# supported platforms, and both used to be reported as "nothing is running out
# of this installation".
_CANNOT_READ_THE_PROCESS_TABLE = (
    "Whether a server of this installation is running could not be read on this host, so whether one is still "
    "answering with an earlier release is not something this result can say."
)


def _still_running_the_previous_version(holders: list[JsonObject] | None, previous_version: str) -> JsonObject:
    """The servers the swap did not reach, and the one thing left to do about them.

    Nothing here is a refusal. A process that was started out of this
    installation keeps the files it has mapped, goes on executing the release it
    imported, and stops doing so when the host that started it restarts, which
    is exactly what `restart_required` already means. So the operator is told
    which hosts to restart and nothing else: pid, image, the project directory
    it was started in and when it started, plus the sentence that says why they
    still answer with the old number.

    A host that could not read its process table gets the sentence that says so
    and no `restart_required`, because "cannot say" is not "no". Empty where the
    table was read and held nothing, so a result gains these fields only where
    they say something.
    """
    if holders is None:
        return {"restart_notice": _CANNOT_READ_THE_PROCESS_TABLE}
    if not holders:
        return {"restart_required": False}
    single = len(holders) == 1
    named = "1 process" if single else f"{len(holders)} processes"
    return {
        "restart_required": True,
        "restart_required_by": holders[:_REPORTED_HOLDER_LIMIT],
        "restart_required_by_count": len(holders),
        "restart_notice": (
            f"{named} started out of this installation before the swap and still {'runs' if single else 'run'} "
            f"{previous_version}: {_named_holders(holders)}. Each goes on answering with {previous_version} "
            f"until the host that started it restarts, and that restart is the whole of what is left to do."
        ),
    }


def _named_holders(holders: list[JsonObject]) -> str:
    """The running processes in one clause, so the summary names them and not a count.

    The reported defect was a result that read the same with a live server and
    with nothing running at all, and a count in a field is not what tells those
    apart on the line a person reads first. Each is named by pid and by the
    directory it was started in, which for an MCP server is the project its host
    opened it for, and the list stops at the same limit the field does.
    """
    named = [
        f"pid {holder['pid']}" + (f" in {holder['working_directory']}" if holder.get("working_directory") else "") + (f", started {holder['started_at']}" if holder.get("started_at") else "")
        for holder in holders[:_REPORTED_HOLDER_LIMIT]
    ]
    rest = len(holders) - len(named)
    return _named_series(named) + (f", and {rest} more under `restart_required_by`" if rest > 0 else "")


def _nothing_new_to_load(holders: list[JsonObject] | None) -> JsonObject:
    """The servers running out of an installation that nothing replaced.

    The other half of the same reported defect: an installation that was already
    current answered `restart_required: false` with a live server up, and that is
    the case where a restart matters most. This call replaced nothing, so what
    the running server imported is whatever was on disk when it started, and
    nothing here can read that from outside. An earlier upgrade is exactly how a
    server comes to be older than the installation it runs out of, so the
    processes are named and the restart is asked for rather than ruled out.

    Three answers, because there are three states and the last two were one:
    processes to name, a table that was read and held none of them, and a host
    that could not read its table at all. The third carries the sentence saying
    so and no `restart_required`, so no result claims a restart is unnecessary on
    the strength of a question nobody could put.
    """
    if holders is None:
        return {"restart_notice": _CANNOT_READ_THE_PROCESS_TABLE}
    if not holders:
        return {"restart_required": False}
    named = "1 process is" if len(holders) == 1 else f"{len(holders)} processes are"
    return {
        "restart_required": True,
        "restart_required_by": holders[:_REPORTED_HOLDER_LIMIT],
        "restart_required_by_count": len(holders),
        "restart_notice": (
            f"{named} running out of this installation: {_named_holders(holders)}. Nothing was replaced by this call, "
            f"so each of them still answers with the release it imported when it started, which is this one only if it "
            f"started after the last upgrade. Restarting the host that started it is what makes that certain."
        ),
    }


def replace_installation(*, tool: str) -> JsonObject:
    """Hand this installation to its own package manager and report what moved.

    The whole of the manager interaction and none of either caller's own
    business: no skill refresh, no permission, no bench check. It raises
    `ConfigError("upgrade_manager_not_found")` rather than returning it, because
    that is the one condition where there is no manager to have an outcome from.
    The command line prints it as a refusal like any other, and the MCP tool
    turns it into a result.

    A server running out of this installation is read here and reported, never
    refused. An upgrade the operator typed at their own shell goes through: the
    running server keeps its mapped files and its old behaviour, the new release
    lands on disk, and the result names the hosts whose restart adopts it. The
    refusal this replaces asked an operator to go and close sessions they could
    not always see, and the half of it that was a real failure -- the launcher
    on PATH that a running process has mapped -- is answered by moving that
    launcher aside rather than by not upgrading.
    """
    still_running = _processes_holding_installation()
    # First, because it is the one step that undoes work an earlier run left
    # behind, and it must happen even on a run that turns out to have nothing to
    # install: a bench that is already current is exactly where the leftovers of
    # the upgrade before it are sitting.
    _remove_superseded_launchers()

    manager, command = _upgrade_command()
    previous_version = __version__
    # Both read before anything runs. They describe the installation as it is
    # now, and the one result that needs them is the one where it is not there
    # any more: rebuilt afterwards from metadata a half-finished manager run
    # removed, the repair command names the bare distribution and takes `[can]`
    # off a bench that had it.
    installed_extras = _installed_extras()
    reinstall_command = reinstall_command_with_extras(installed_extras)

    skipped, resolution, resolution_certificates = _would_install_nothing(manager, command)
    if skipped:
        # The certificate note belongs on this outcome and on no other. Here the
        # resolution query is the only thing that ran, so what it met is the
        # whole of what this call knows about the proxy. On the fall-through the
        # install runs next and meets the same network, and its own note is the
        # one that reports it; carrying this one there as well would put two
        # accounts of one proxy on a single result.
        return _with_certificate_note(_nothing_to_install(tool, manager, command, previous_version, resolution, still_running), resolution_certificates)

    # The launchers on PATH go out of the manager's way here and nowhere
    # earlier: an already-current run would have renamed the installation's own
    # entry points aside and given nothing back, because the manager it never
    # reached is what writes the replacements.
    moved_aside = _launchers_moved_aside()
    try:
        installed, install_result, certificates = _manager_run(manager, command)
    except (OSError, subprocess.TimeoutExpired) as error:
        restore = _restore_unreplaced_launchers(moved_aside)
        return _with_unrecovered_launchers(
            _failed_upgrade(
                tool,
                manager,
                command,
                previous_version,
                installed_extras,
                reinstall_command,
                summary="Agentic HIL package upgrade could not run.",
                exception_type=type(error).__name__,
                detail=str(error),
            ),
            restore,
        )
    except BaseException as error:
        # A KeyboardInterrupt, a SystemExit, or any unexpected error from the
        # manager run would otherwise skip restoration entirely and leave the
        # launchers renamed aside -- the CLI off PATH with only a `.superseded`
        # sibling. Put them back before the exception propagates, so an
        # interrupted upgrade never disarms the very command used to repair it
        # (review round 0, finding 4).
        restore = _restore_unreplaced_launchers(moved_aside)
        if restore.unrecovered:
            # Restoration itself failed while the run was being interrupted, so
            # re-raising would carry the interruption away and leave the CLI off
            # PATH with no cleanup-required result and no recovery path -- the
            # exact case round 0, finding 4 required to be surfaced rather than
            # buried, now closed on the exceptional path too (review round 1,
            # finding 3). Return the structured cleanup failure naming each
            # launcher and its `recover_from` sibling, recording the interruption
            # that caused it instead of dropping it. The original exception is not
            # re-raised here on purpose: the operator must be told how to put the
            # launcher back, and an interruption that propagated silently would be
            # the disarmed-repair-command bug all over again.
            return _with_unrecovered_launchers(
                _failed_upgrade(
                    tool,
                    manager,
                    command,
                    previous_version,
                    installed_extras,
                    reinstall_command,
                    summary="Agentic HIL package upgrade was interrupted before it finished.",
                    exception_type=type(error).__name__,
                    detail=str(error),
                ),
                restore,
            )
        # Every launcher went back, so nothing is left off PATH: the interruption
        # has cost the run nothing a result would need to carry, and it propagates
        # exactly as it would have (review round 0, finding 4).
        raise
    restore = _restore_unreplaced_launchers(moved_aside)
    superseded = restore.superseded
    if installed.returncode != 0:
        return _with_unrecovered_launchers(
            _with_certificate_note(
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
            ),
            restore,
        )

    verification, current_version = _loaded_version()
    if current_version is None:
        # A manager that exited zero and left nothing that loads is the same end
        # state as one that failed, reported the same way: the previous outcome
        # here named the verification step and stopped, which told an operator
        # that something could not be loaded without telling them their
        # installation was gone or what single command brings it back.
        return _with_unrecovered_launchers(
            _with_certificate_note(
                _installation_broken(
                    {"ok": False, "tool": tool, "manager": manager, "command": command, "python": sys.executable, "previous_version": previous_version, "install": install_result},
                    installed_extras,
                    reinstall_command,
                    verification,
                    "Agentic HIL package manager completed, but the installation it left cannot be loaded.",
                ),
                certificates,
            ),
            restore,
        )

    if current_version == previous_version:
        return _with_unrecovered_launchers(_with_certificate_note(_upgrade_changed_nothing(tool, manager, command, previous_version, current_version, install_result, still_running), certificates), restore)

    return _with_unrecovered_launchers(
        _with_certificate_note(
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
                # What is left to restart, and nothing more: the servers found
                # running out of this installation. A caller that knows about a
                # host with the server registered raises it from there, which is
                # the command line, because a registration is a file this module
                # does not read. An upgrade on a machine with neither had been
                # asking for a restart of nothing, and one on a host that cannot
                # read its process table says so rather than answering false.
                **({"superseded_entrypoints": superseded} if superseded else {}),
                **_still_running_the_previous_version(still_running, previous_version),
            },
            certificates,
        ),
        restore,
    )


# ---------------------------------------------------------------------------
# The MCP tool.
#
# Three gates before the shared implementation above, in this order and for this
# reason. The permission is first because it is the operator's standing policy
# and the answer does not depend on anything else: a bench whose
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
        # The dotted key `agentic-hil grant` takes, not the bare `allow_upgrade`
        # that `resolve_permission_key` rejects; the short name stays only as the
        # scope the catalogue is looked up by below (round 1, finding 3).
        "permission": "permissions.allow_upgrade",
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

    The one guard that survives the operator's rule, because it is not about
    other people's servers but about this one. The interpreter executing this
    call is inside the environment the manager has to remove, and Windows
    refuses to delete a file mapped as a running image: a manager that removes
    the environment before it rebuilds it fails on that delete and stops in
    between. What the command line does about the launcher on PATH does not
    reach this, and could not: renaming a file inside a directory does not let
    that directory be removed, and the one image in the way here is the
    interpreter running the code that would do the renaming.

    The issue allowed two answers (a detached helper that swaps once this server
    has exited, or a named refusal) and forbade the third, a swap that half
    happens. The refusal is the one that can be told the truth about. A helper
    outlives the result that announced it: whatever it does afterwards happens
    with nobody left to report it, so the tool would have to promise an outcome
    it cannot observe, and a helper that failed would leave exactly the
    half-replaced environment the promise was made to avoid. `agentic-hil
    upgrade` at a shell is a process that is not the server, so it can watch its
    own manager finish and report what actually happened.
    """
    return {
        "ok": False,
        "tool": SERVER_UPGRADE,
        "error_type": "upgrade_cli_only_on_host",
        "summary": (
            "This host is Windows, where a file mapped as a running image cannot be replaced, and this server is "
            "running out of the installation the upgrade would replace. A package manager that removes the environment "
            "before it rebuilds it fails on that delete and stops in between, leaving neither the old installation nor "
            "the new one, so nothing was attempted. On this host the upgrade is the command line's: `agentic-hil "
            "upgrade` runs as a separate process, so the environment it replaces is not the one it is running out of. "
            "It keeps the extras this installation was created with, it is not refused because this server is running, "
            "and it names this server among the processes that answer with the old release until the host restarts."
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
    that release mid-run moves the rules during the run they govern: the same
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
            "Something is holding this bench right now (a declared run, an open COM or CAN session, or a debug "
            "session), and those holds were taken under the release this server is running. Replacing the code "
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
    "Ask the operator to restart the MCP server: the one the agent host started for this workspace. That is what "
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
    read here, because the coordinator that can answer it is the service's own:
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
    on every outcome, equal to `version` when nothing moved, and deliberately
    behind it when something did.
    """
    reported: JsonObject = {**result, "running_version": __version__}
    extras = missing_configured_extras(config)
    if extras is not None:
        reported["extras_warning"] = extras
    if not result.get("upgraded_on_disk"):
        return reported
    # This server is the running server, and it is the one process the list of
    # them cannot contain: `_processes_holding_installation` excludes the process
    # doing the upgrading, which over MCP is this one. So the restart is required
    # here whatever that list found.
    reported["restart_required"] = True
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
