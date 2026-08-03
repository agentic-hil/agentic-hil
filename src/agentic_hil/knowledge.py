"""The one source for everything the server knows about itself.

Two consumers read this module, and that is the point of it existing:

* every failing result that carries a concrete fix (``remediation_fields``), so
  the answer arrives at the moment of the failure and nothing has to be looked
  up, and
* the MCP resources (``MCP_RESOURCES``, ``read_resource``), so a caller who
  installed the distribution with ``uvx`` or ``uv tool install`` and has no
  source tree can still answer "which fields does this backend need", "what do I
  do about this error_type", and "where may state_root live" over the connection
  that is already open.

Measured, the alternative is real: agents read the installed package under
``site-packages/agentic_hil`` to recover facts nobody published. Anything a
caller must know therefore belongs here as data, never only in a code path.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from functools import cache, lru_cache
from importlib import resources
from pathlib import Path

from agentic_hil.types import JsonObject

RESOURCE_SCHEME = "agentic-hil"

CONFIG_SCHEMA_URI = f"{RESOURCE_SCHEME}://reference/config-schema"
CONFIG_SHAPE_URI = f"{RESOURCE_SCHEME}://reference/config-shape"
DEBUGGER_BACKENDS_URI = f"{RESOURCE_SCHEME}://reference/debugger-backends"
ERRORS_URI = f"{RESOURCE_SCHEME}://reference/errors"
LEASE_LIFECYCLE_URI = f"{RESOURCE_SCHEME}://reference/lease-lifecycle"
PLATFORM_PATHS_URI = f"{RESOURCE_SCHEME}://reference/platform-paths"
TARGET_SUPPORT_URI = f"{RESOURCE_SCHEME}://reference/target-support"

ERROR_URI_PREFIX = f"{ERRORS_URI}/"
DEBUGGER_BACKEND_URI_PREFIX = f"{DEBUGGER_BACKENDS_URI}/"

JSON_MIME = "application/json"
MARKDOWN_MIME = "text/markdown"

BACKENDS = ("openocd", "stlink", "pyocd")


@dataclass(frozen=True)
class ErrorRemedy:
    """What an error_type means and what the caller does about it.

    ``remediation`` is ordered: the first step is the one that fixes the case
    that actually occurs. ``do_not`` names the wrong fix that looks right, and
    exists because a caller that is only told "no" reaches for the workaround
    the refusal was protecting against.
    """

    meaning: str
    remediation: tuple[str, ...]
    do_not: tuple[str, ...] = ()

    def as_json(self) -> JsonObject:
        payload: JsonObject = {"meaning": self.meaning, "remediation": list(self.remediation)}
        if self.do_not:
            payload["do_not"] = list(self.do_not)
        return payload


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def safe_user_root() -> str:
    """A directory whose every ancestor passes the trust check on both platforms.

    On a Windows profile where an installed packaged application holds an
    app-capability ACE (``S-1-15-3-*``, FullControl) on ``%USERPROFILE%\\AppData``,
    ``Roaming`` and ``Local`` inherit it and both fail the ancestor check while
    the profile root itself passes.
    """
    return str(_home() / ".agentic-hil")


def safe_state_root_suggestion() -> str:
    return str(Path(safe_user_root()) / "state")


def safe_user_config_suggestion() -> str:
    return str(Path(safe_user_root()) / "projects" / "<workspace-name>" / "config.yaml")


def _substitutions() -> dict[str, str]:
    return {
        "safe_user_root": safe_user_root(),
        "safe_state_root": safe_state_root_suggestion(),
        "safe_user_config": safe_user_config_suggestion(),
    }


# Keys are "<error_type>" or "<error_type>:<scope>", where scope is the config
# field the error names or the debugger backend that raised it. Lookup falls back
# from the scoped key to the bare one, so a scope nobody wrote an entry for still
# gets the general fix instead of nothing.
ERROR_CATALOGUE: dict[str, ErrorRemedy] = {
    "installation_in_use": ErrorRemedy(
        meaning=(
            "A process is running out of the installation this upgrade would replace, and on Windows a file mapped as "
            "a running image cannot be deleted. A package manager that removes the environment before it rebuilds it "
            "fails on that delete and stops in between, leaving neither the old installation nor the new one. The "
            "refusal comes before anything is removed, so the installation is untouched and still works."
        ),
        remediation=(
            "Close the agent host. It starts the Agentic HIL MCP server itself, so that server runs for as long as "
            "the host does — which is why a working setup is exactly the state this fails in.",
            "Run `agentic-hil upgrade` again, then start the host, which picks up the new server.",
            "If no host is open, the refusal names each holding process by pid and image path. End those processes, "
            "then run the upgrade again.",
        ),
        do_not=(
            "Do not reach for `uv tool install --upgrade` or `uv tool install --force` to get past this. Those are "
            "the commands the refusal protects you from: they remove the environment first, and that removal is what "
            "fails while the server holds it.",
            "Do not delete the environment by hand to unblock the upgrade. The running server is what holds it, and "
            "an installation destroyed around a live process is the outcome being refused.",
        ),
    ),
    "config_file_not_found": ErrorRemedy(
        meaning=(
            "This workspace has no authoritative configuration, so there is no bench, no permission and no state "
            "directory for the call to use. It is the first wall a new project hits, not a fault."
        ),
        remediation=(
            "Over MCP, call `project_config_create` once. It takes no arguments, generates the configuration out of "
            "the hardware attached to this machine, and writes every permission false — including the one that would "
            "let it write the file again.",
            "On the command line a person runs `agentic-hil init` from the project root, which does the same thing.",
            "Then ask the operator to set the permissions the task actually needs; nothing but a human edit turns one "
            "of them true.",
        ),
        do_not=(
            "Do not write the configuration by hand to give yourself a permission, and do not drive the hardware "
            "another way while the project has none. A missing configuration is the absence of policy, not permission "
            "to act without it.",
            "Do not delete or move an existing configuration to reach this state. Generating a replacement produces "
            "the deny-by-default skeleton again, so it throws the operator's settings away and gives you nothing.",
        ),
    ),
    "permission_denied:allow_config_description_write": ErrorRemedy(
        meaning=(
            "A field-wise configuration change reached a description key — what the bench is — and "
            "`permissions.allow_config_description_write` is false in the authoritative configuration. `denied_keys` "
            "lists exactly which keys were refused. Nothing was written."
        ),
        remediation=(
            "Report the refusal, name `permissions.allow_config_description_write`, and say which file carries it "
            "(`path` in the refusal). Then stop; an operator decides this.",
            "Call `project_config_describe` to see which keys this configuration does leave open right now, so the "
            "part of the task that is possible is not abandoned with the part that is not.",
            "What this grant opens, and what the other one opens: MCP resource " + CONFIG_SHAPE_URI + ".",
        ),
        do_not=(
            "Do not edit the configuration with your own file tools. `agentic-hil setup` writes host deny rules "
            "against exactly that, and this refusal is the reason they exist.",
            "Do not set the grant through `project_config_set` either. It is a permission, so it needs "
            "`allow_config_permissions_write`, which no configuration hands out to close its own refusal.",
        ),
    ),
    "permission_denied:allow_config_permissions_write": ErrorRemedy(
        meaning=(
            "A field-wise configuration change reached a `permissions:` block — what the bench may be told to do — and "
            "`permissions.allow_config_permissions_write` is false. This is the deliberate half of the split: a "
            "configuration may be open for describing the bench and closed for granting, and this refusal is that "
            "state working as intended. `denied_keys` lists the refused keys. Nothing was written."
        ),
        remediation=(
            "Report the refusal, name `permissions.allow_config_permissions_write`, and stop. Granting is an "
            "operator's decision and this refusal is the answer to the request, not an obstacle in front of it.",
            "If the task was to describe the bench rather than to widen it, re-send only the description keys; "
            "`project_config_describe` says which ones are open.",
        ),
        do_not=(
            "Do not route the change through another key to reach a permission — a whole subtree, a differently "
            "spelled path. Values are scalars only and the permissions present in the document are compared before "
            "and after every write, so it fails, and it is the thing this grant exists to prevent.",
            "Do not carry out the action the permission would have allowed by another route. A debugger, serial "
            "device or CAN adapter driven outside Agentic HIL defeats the policy this refusal enforces.",
        ),
    ),
    "config_write_in_open_run": ErrorRemedy(
        meaning=(
            "A configuration write was attempted while this server holds hardware: a declared run, an open COM or CAN "
            "session, or a debug session. Those holds were taken under the policy this file states, so changing it "
            "underneath them would move the rules during the run they govern. Nothing was written and the run is "
            "untouched."
        ),
        remediation=(
            "Finish the run and close it with `bench_run_stop`, stop any COM or CAN session with "
            "`com_session_stop` / `can_session_stop` and any debug session with `debug_stop_session`, then repeat the "
            "configuration change.",
            "`bench_run_status` says whether a run is open and which devices it declared; the refusal carries the "
            "same in `open_holds`.",
        ),
        do_not=(
            "Do not end a run early only to get the write through. The run is holding a board for a reason, and a "
            "configuration change is never urgent enough to abandon hardware in an unconfirmed state.",
        ),
    ),
    "unsafe_configured_path": ErrorRemedy(
        meaning=(
            "A configured path, or one of its ancestor directories, can be replaced or re-permissioned by a principal "
            "other than the current user, SYSTEM, or Administrators. The validator only reads ACLs; it never changes them."
        ),
        remediation=(
            "Move the path under {safe_user_root}, whose every ancestor passes the check on a stock profile.",
            "Windows: %USERPROFILE% passes; %APPDATA% and %LOCALAPPDATA% do not on a profile where AppData carries an "
            "app-capability ACE (S-1-15-3-*, FullControl) that both inherit.",
            "On Windows the refusal carries `untrusted_principals`: the SID that holds the right, and the application "
            "package it belongs to when the SID resolves. That names who to ask, so the choice between moving the path "
            "and having the operator revoke the grant is an informed one.",
            "Re-run the command that failed. Nothing else has to change.",
            "Full rules and the measured per-directory verdicts: MCP resource " + PLATFORM_PATHS_URI + ".",
        ),
        do_not=(
            "Do not change the ACL or the owner of the rejected path or any ancestor. That removes the protection the "
            "check exists for instead of satisfying it.",
        ),
    ),
    "unsafe_configured_path:user_config": ErrorRemedy(
        meaning=(
            "The directory or file holding the authoritative configuration has an untrusted ancestor. On a Windows 11 "
            "profile where an installed packaged application holds an app-capability ACE on AppData, the documented "
            "default under %APPDATA% is exactly this case. On Windows the refusal names the holder in "
            "`untrusted_principals` when the SID resolves to a package."
        ),
        remediation=(
            "AGENTIC_HIL_CONFIG is the supported answer here, not a workaround: it is how a project binds to a "
            "configuration outside the discovered default, and a refused default is precisely what it is for.",
            "Create {safe_user_root}/projects/<workspace-name>/ and move config.yaml there.",
            "Point the server at it with an absolute path: AGENTIC_HIL_CONFIG={safe_user_config}",
            "Set AGENTIC_HIL_CONFIG in the host's user-level or managed environment, never in a repository-controlled "
            "file such as .vscode/mcp.json, .mcp.json, .codex/config.toml, or opencode.json.",
            "Re-run `agentic-hil doctor` to confirm the new location loads.",
        ),
        do_not=(
            "Do not change the ACL or the owner of %APPDATA%, %LOCALAPPDATA%, or AppData itself. The grant belongs to "
            "an installed application, which re-applies it; relaxing it weakens every application in the profile.",
        ),
    ),
    "unsafe_configured_path:state_root": ErrorRemedy(
        meaning=(
            "state_root, one of its ancestors, or a directory derived from it failed the trust check. state_root holds "
            "hardware leases, quarantine incidents, and canonical reports, so a replaceable ancestor would let another "
            "local principal forge them."
        ),
        remediation=(
            "Set `state_root: {safe_state_root}` in the authoritative configuration. state_root is a first-class "
            "setting with no fixed location: any absolute directory that passes the check is a supported value, and "
            "choosing one is the answer to a refused default rather than a workaround for it.",
            "state_root must be absolute and must not overlap workspace_root in either direction.",
            "`agentic-hil init` derives the default state_root from %LOCALAPPDATA% on Windows and $XDG_STATE_HOME on "
            "POSIX, and validates before writing anything. On a stock Windows profile that default is rejected, so it "
            "cannot produce a usable file: write the configuration at {safe_user_config} yourself with "
            "`state_root: {safe_state_root}` (every field is in MCP resource "
            + CONFIG_SCHEMA_URI
            + "), then set AGENTIC_HIL_CONFIG to that absolute path.",
            "This is the project half only: `agentic-hil agent-install` reads and writes no configuration and no "
            "state_root, so on such a profile the agent skill and the user-level MCP registration install normally "
            "and only this project binding is left.",
            "Re-run `agentic-hil doctor`; it reports the first path that still fails.",
        ),
        do_not=(
            "Do not change the ACL or the owner of the rejected directory, and do not move state_root inside "
            "workspace_root to dodge the check.",
        ),
    ),
    "unsafe_configured_path:device_lock_root": ErrorRemedy(
        meaning=(
            "The machine-wide device lock directory, ~/.agentic-hil/device-locks, or one of its ancestors failed the "
            "trust check. That directory is the single place every process on this machine agrees to look for who holds "
            "a board, so a replaceable ancestor would let another local principal fake or steal a hold."
        ),
        remediation=(
            "Fix the ancestor the refusal names under {safe_user_root}; the location is fixed and has no override, "
            "because an override is how two sessions stop seeing each other.",
            "Windows: %USERPROFILE% passes; the failure is normally a directory somebody re-permissioned below it.",
            "Full rules and the measured per-directory verdicts: MCP resource " + PLATFORM_PATHS_URI + ".",
        ),
        do_not=(
            "Do not change the ACL of the rejected path, and do not work around it by keeping the lock beside the "
            "configuration: a lock kept per configuration is not a bench lock, which is exactly the failure this "
            "location exists to prevent.",
        ),
    ),
    "device_busy": ErrorRemedy(
        meaning=(
            "A physical device is held by another owner for the duration of their run. The refusal names the holder in "
            "`holder` (pid, host, frontend, and the run label when there is one) and when it took the device in "
            "`held_since`. Nothing was touched."
        ),
        remediation=(
            "Read `holder` and wait for that run, or ask its owner to finish. This is not a fault: it is the exclusivity "
            "that replaced the read permission.",
            "If waiting is the right answer, ask for it explicitly and bounded: `wait_s` on the run start. Waiting is "
            "never silent and never unbounded.",
            "A holder whose `heartbeat_age_s` is large and `holder_heartbeat_stale` is true is hung rather than busy; "
            "the hold is still real, so stop that process rather than deleting anything.",
        ),
        do_not=(
            "Do not delete the lock file, and do not retry in a loop. The hold belongs to a live process; removing it "
            "would let two runs drive one board, which is the failure the mutex exists to prevent.",
        ),
    ),
    "undeclared_device": ErrorRemedy(
        meaning=(
            "A run reached for a device its test description does not name. `declared_devices` lists what it declared, "
            "`undeclared_devices` what it reached for. Nothing was touched."
        ),
        remediation=(
            "Add the device to the test description and rerun. The declaration is what the mutex locks before the run "
            "starts, so a device that is not declared was never locked and could be driven by somebody else mid-run.",
            "A test plan declares a debugger with `debugger: <name>` and a serial line with `port_id: <name>` on the "
            "steps that use them.",
        ),
        do_not=(
            "Do not work around this by splitting the access into a separate session outside the run. That is exactly "
            "the outside observation the declaration exists to keep out of a running test.",
        ),
    ),
    "run_already_active": ErrorRemedy(
        meaning="This owner already holds an open run, and a run declares its devices once, up front.",
        remediation=(
            "End the open run before declaring another; the devices it declared are released then.",
            "One run per owner is what makes the declared set the complete answer to what this owner may touch.",
        ),
    ),
    "target_not_detected:openocd": ErrorRemedy(
        meaning="OpenOCD reached the debug adapter but no target answered on the selected transport.",
        remediation=(
            "Confirm the probe enumerates at all: call debugger_probes_list. An empty list means the probe is missing, "
            "not the target.",
            "Check `debuggers.<name>.target_cfg` names the MCU family on the board. The default `target/stm32f4x.cfg` "
            "covers STM32F4 only.",
            "Check `debuggers.<name>.interface_cfg` matches the probe: `interface/stlink.cfg` for an on-board ST-Link.",
            "Confirm the board is powered from a source the probe can see, and that SB/solder-bridge jumpers for SWD "
            "are populated.",
            "Release the probe from any other session: a second OpenOCD, STM32CubeIDE, or STM32CubeProgrammer holds it "
            "exclusively.",
            "If the running firmware disables SWD or enters a low-power mode, connect while the board is held in reset.",
        ),
    ),
    "target_not_detected:stlink": ErrorRemedy(
        meaning="STM32CubeProgrammer reached the ST-Link but no target answered.",
        remediation=(
            "Confirm the probe enumerates at all: call debugger_probes_list. An empty list means the probe is missing, "
            "not the target.",
            "Check `debuggers.<name>.interface` is the transport that is actually wired: SWD or JTAG. It is passed as "
            "`port=`.",
            "Confirm the board is powered and the ST-Link firmware is current; STM32CubeProgrammer refuses old ST-Link "
            "firmware against newer parts.",
            "Release the probe from any other session (STM32CubeIDE, a second STM32_Programmer_CLI, OpenOCD).",
            "If the running firmware disables SWD or enters a low-power mode, connect while the board is held in reset.",
        ),
    ),
    "target_not_detected:pyocd": ErrorRemedy(
        meaning="pyOCD reached the probe but could not attach to the target.",
        remediation=(
            "Confirm the probe enumerates at all: call debugger_probes_list. An empty list means the probe is missing, "
            "not the target.",
            "Set `debuggers.<name>.target_type` to the MCU part. Without it pyOCD guesses from the probe's board ID and "
            "the guess is wrong for a custom board.",
            "Confirm the value is one this pyOCD resolves; most STM32 parts exist only after a CMSIS pack is installed. "
            "See MCP resource " + TARGET_SUPPORT_URI + ".",
            "Confirm the board is powered and the SWD/JTAG wiring is intact.",
            "Release the probe from any other session (OpenOCD, STM32CubeProgrammer, a second pyOCD).",
        ),
    ),
    "adapter_not_found:openocd": ErrorRemedy(
        meaning="OpenOCD could not open a debug adapter.",
        remediation=(
            "Call debugger_probes_list to see what the host enumerates.",
            "Connect the probe, or on Windows bind the correct USB driver to it (ST-Link needs the ST driver, not "
            "WinUSB, unless the config selects a WinUSB interface).",
            "Set `debuggers.<name>.probe_id` to the serial number of the intended probe when more than one is attached; "
            "it is passed as `adapter serial`.",
            "Close whatever else holds the probe.",
        ),
    ),
    "adapter_not_found:stlink": ErrorRemedy(
        meaning="STM32CubeProgrammer could not open an ST-Link.",
        remediation=(
            "Call debugger_probes_list to see what the host enumerates.",
            "Set `debuggers.<name>.probe_id` to the ST-Link serial number reported there; it is passed as `sn=`.",
            "Connect the probe, or install the ST-Link USB driver shipped with STM32CubeProgrammer.",
            "Close whatever else holds the probe.",
        ),
    ),
    "adapter_not_found:pyocd": ErrorRemedy(
        meaning="pyOCD could not open a probe, or the configured selector matched none.",
        remediation=(
            "Call debugger_probes_list to see the unique IDs this host enumerates.",
            "Set `debuggers.<name>.probe_id` to a full unique ID. pyOCD matches --uid as a case-insensitive substring, "
            "so a shortened value can select a board you did not name.",
            "Connect the probe, or install the udev rule (Linux) or USB driver (Windows) for it.",
            "Close whatever else holds the probe.",
        ),
    ),
    "debugger_command_rejected:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD refused a command in its own interpreter and stopped before it opened the debug probe. The named "
            "command either does not exist in this OpenOCD build, or it belongs to the run stage and was reached before "
            "`init`. Nothing was sent to the bench, so the target is exactly as the last call that did reach it left it."
        ),
        remediation=(
            "Read `rejected_commands` in the result: those are the commands OpenOCD would not run.",
            "Check `debuggers.<name>.interface_cfg` and `target_cfg` for a script that uses a run-stage command such as "
            "`reset`, `halt` or `mww` before `init`. OpenOCD registers those only while `init` runs.",
            "Confirm the installed OpenOCD is a release that knows the command: `debugger_info` reports the version.",
            "Retry the call once the cause is fixed. The bench was not driven, so nothing has to be inspected or "
            "recovered first.",
        ),
        do_not=(
            "Do not inspect the hardware or run `agentic-hil recover` for this result. It is a rejected call, not an "
            "unconfirmed target state, and the two must not be treated the same.",
        ),
    ),
    "target_type_invalid:pyocd": ErrorRemedy(
        meaning=(
            "pyOCD does not resolve `debuggers.<name>.target_type`. Most vendor parts are not built into pyOCD; they "
            "come from an installed CMSIS device-family pack."
        ),
        remediation=(
            "Check the spelling against `pyocd list --targets`; the Source column says `builtin` or `pack`.",
            "If the part is not listed, install its pack as a deliberate host setup step: `pyocd pack find <part>` then "
            "`pyocd pack install <target_type>`. The exact commands, with the configured value already substituted, "
            "are in `install_commands` on the result.",
            "Run them yourself. `pyocd pack install` downloads a device-family pack from the vendor index over the "
            "network; Agentic HIL names that command and never runs it.",
            "`agentic-hil doctor` answers the same question before anything is flashed, in "
            "`debuggers.<name>.target_support`.",
            "Re-run the failing tool afterwards. Provenance and where installed packs live: MCP resource "
            + TARGET_SUPPORT_URI
            + ".",
        ),
        do_not=(
            "Do not reconstruct the pack by downloading .pdsc or .pack files by hand. That is a network fetch nobody "
            "reviewed, of content that then sits outside the cache pyOCD reads, and it is what happened the last time "
            "this was undocumented.",
        ),
    ),
}


def remediation_fields(error_type: str | None, scope: str | None = None) -> JsonObject:
    """The remediation fields for an error, or an empty object when none is known.

    Merge the result into a failing payload. Empty for every error_type the
    catalogue does not cover, so callers can apply it unconditionally without
    inventing advice for errors nobody has written a fix for.
    """
    remedy = lookup_remedy(error_type, scope)
    if remedy is None:
        return {}
    values = _substitutions()
    payload: JsonObject = {"remediation": [step.format(**values) for step in remedy.remediation]}
    if remedy.do_not:
        payload["do_not"] = [step.format(**values) for step in remedy.do_not]
    return payload


def lookup_remedy(error_type: str | None, scope: str | None = None) -> ErrorRemedy | None:
    if not error_type:
        return None
    if scope:
        scoped = ERROR_CATALOGUE.get(f"{error_type}:{scope}")
        if scoped is not None:
            return scoped
    return ERROR_CATALOGUE.get(error_type)


def catalogue_entry(key: str) -> JsonObject | None:
    remedy = ERROR_CATALOGUE.get(key)
    if remedy is None:
        return None
    error_type, _, scope = key.partition(":")
    values = _substitutions()
    entry: JsonObject = {"error_type": error_type}
    if scope:
        entry["scope"] = scope
    entry["meaning"] = remedy.meaning
    entry["remediation"] = [step.format(**values) for step in remedy.remediation]
    if remedy.do_not:
        entry["do_not"] = [step.format(**values) for step in remedy.do_not]
    return entry


# Exactly what each backend reads out of a `debuggers.<name>` entry. "required"
# means the backend cannot work without it; "discovered" means it is found
# automatically when unset; "ignored" means the backend never reads it, so
# setting it changes nothing.
DEBUGGER_FIELD_MATRIX: JsonObject = {
    "openocd": {
        "tool": "openocd",
        "type": {"status": "optional", "value": "openocd", "note": "Default. Omit only if no other backend is meant."},
        "executable": {"status": "discovered", "note": "Falls back to `openocd` on PATH. An absolute path or a value containing a separator is resolved against workspace_root and must exist."},
        "probe_id": {"status": "optional", "note": "Adapter serial number, passed as `adapter serial <probe_id>`. Required once more than one debugger is configured."},
        "target_type": {"status": "ignored", "note": "OpenOCD selects the target through target_cfg."},
        "interface": {"status": "ignored", "note": "OpenOCD selects the transport through interface_cfg."},
        "interface_cfg": {"status": "required", "default": "interface/stlink.cfg", "note": "OpenOCD script, passed as `-f`. Resolved against OpenOCD's own search path."},
        "target_cfg": {"status": "required", "default": "target/stm32f4x.cfg", "note": "OpenOCD script, passed as `-f`. Must match the MCU family."},
        "flash_address": {"status": "ignored", "note": "OpenOCD takes the load address from the image."},
    },
    "stlink": {
        "tool": "STM32_Programmer_CLI (STM32CubeProgrammer)",
        "type": {"status": "required", "value": "stlink"},
        "executable": {"status": "discovered", "note": "Falls back to STM32_Programmer_CLI on PATH, then the standard STM32CubeProgrammer and STM32CubeIDE install locations."},
        "probe_id": {"status": "optional", "note": "ST-Link serial number, passed as `sn=<probe_id>`. Required once more than one debugger is configured."},
        "target_type": {"status": "ignored", "note": "STM32CubeProgrammer identifies the part itself."},
        "interface": {"status": "required", "default": "SWD", "enum": ["SWD", "JTAG"], "note": "Passed as `port=<interface>`."},
        "interface_cfg": {"status": "ignored"},
        "target_cfg": {"status": "ignored"},
        "flash_address": {"status": "conditional", "note": "Required to flash a .bin, which carries no load address. Not read for .elf or .hex. Pattern: 0x-prefixed hex or decimal, e.g. 0x08000000."},
    },
    "pyocd": {
        "tool": "pyocd",
        "type": {"status": "required", "value": "pyocd"},
        "executable": {"status": "discovered", "note": "Falls back to `pyocd` on PATH. Install with `pip install agentic-hil[pyocd]` or `pip install pyocd`."},
        "probe_id": {"status": "optional", "note": "Probe unique ID, passed as `--uid`. pyOCD matches it as a case-insensitive substring and strips a leading `<type>:`, so give the full ID. Required once more than one debugger is configured."},
        "target_type": {"status": "required", "note": "Passed as `--target`. Omitted entirely when unset, leaving pyOCD to guess from the probe's board ID. Most vendor parts resolve only after a CMSIS pack is installed."},
        "interface": {"status": "ignored"},
        "interface_cfg": {"status": "ignored"},
        "target_cfg": {"status": "ignored"},
        "flash_address": {"status": "conditional", "note": "Required to flash a .bin, which carries no load address; passed as `--base-address`. Not read for .elf or .hex."},
    },
}

MULTI_PROBE_RULE = {
    "rule": "probe_id is mandatory for every entry once `debuggers` holds more than one entry.",
    "why": "probe_id is the only field that selects a physical probe. resource_id renames the coordination lease and never substitutes for one; two entries that resolve to one probe would let a plan that says 'flash board_b' flash board_a.",
    "enforced_at": "config load, as error_type `config_invalid`",
    "rejected": [
        "two entries with the same probe_id",
        "one probe_id that is a substring of another when either entry is type pyocd",
        "two entries that resolve to the same coordination resource (same resource_id, or no probe_id and the same executable or type)",
    ],
}

FLASH_ADDRESS_RULE = {
    "rule": "flash_address is required only for a .bin artifact on backends stlink and pyocd.",
    "why": ".bin carries no load address. .elf and .hex do, and the field is not read for them.",
    "failure_when_missing": "error_type `invalid_argument` from flash_firmware, before anything reaches the target.",
    "example": "0x08000000 for STM32 internal flash.",
}


def debugger_backends_document() -> JsonObject:
    return {
        "title": "Required fields per debugger backend",
        "config_path": "debuggers.<name>.<field>",
        "status_legend": {
            "required": "the backend cannot work without it",
            "conditional": "required only in the case named in the note",
            "optional": "read when set, and the backend works without it",
            "discovered": "found automatically when unset",
            "ignored": "never read by this backend",
        },
        "backends": DEBUGGER_FIELD_MATRIX,
        "probe_id_when_multiple_probes": MULTI_PROBE_RULE,
        "flash_address": FLASH_ADDRESS_RULE,
        "permissions": {
            "rule": (
                "Reading a target needs no permission: probing it, listing probes, and opening a debug session are "
                "allowed as configured. Every action that writes or changes state is denied unless "
                "`debuggers.<name>.permissions` grants it. The permissions belong to the operator."
            ),
            "fields": ["allow_flash", "allow_reset", "allow_raw_debugger_commands", "allow_mass_erase"],
            "default": False,
            "reading": (
                "What protects a read is exclusivity, not a grant: every device a run declares is locked machine-wide "
                "for the whole run, and a device the description does not name is refused. See MCP resource "
                + LEASE_LIFECYCLE_URI
                + "."
            ),
            "removed_field": {
                "allow_probe": (
                    "Version 1 only. A configuration that sets `version: 2` must not carry it; a configuration without "
                    "a `version` key is still read under version 1, where reading needs it. Both COM ports and CAN "
                    "buses lost `permissions.allow_read` the same way."
                )
            },
            "on_refusal": "error_type `permission_denied`: report it and stop. Never edit the authoritative configuration to grant it, and never carry the action out another way.",
        },
        "full_schema": CONFIG_SCHEMA_URI,
    }


def errors_document() -> JsonObject:
    return {
        "title": "Agentic HIL error types with remediation",
        "lookup": "Key is error_type, optionally scoped by the config field or debugger backend after a colon. A result carries the scoped remediation inline; this is the same content.",
        "single_entry_uri": ERROR_URI_PREFIX + "{error_type}",
        "result_fields": {
            "error_type": "stable machine-readable identifier; branch on this",
            "backend_error_type": "the backend's own finer classification, when it has one",
            "likely_causes": "what may have caused it",
            "remediation": "ordered steps that fix it",
            "do_not": "the wrong fix that looks right",
            "log_path": "raw backend output",
            "report_path": "canonical structured report",
        },
        "entries": [catalogue_entry(key) for key in sorted(ERROR_CATALOGUE)],
    }


def config_schema_text() -> str:
    return resources.files("agentic_hil").joinpath("schemas", "config.schema.json").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def config_schema_document() -> JsonObject:
    """The shipped schema, parsed once.

    Cached because resolving a key reads it, and a change set resolves many; the
    file is packaged data that cannot change while the process runs. Callers that
    hand a node onward copy it — ``config_key_schema`` does — so nothing that
    escapes into a result can write back into the cache."""
    document = json.loads(config_schema_text())
    if not isinstance(document, dict):  # pragma: no cover - the shipped schema is an object
        raise ValueError("The bundled configuration schema is not a JSON object.")
    return document


# ---------------------------------------------------------------------------
# Which configuration keys an agent may set over MCP, and which grant opens each.
#
# Two rights, not one. One right for everything would be a master key: whoever
# set it so an agent could enter a 24-character probe serial would have handed
# over, in the same motion and without being told, the ability for that agent to
# write `allow_flash: true` on itself.
#
#   description  what the bench IS   — target, probe identity, port parameters
#   permissions  what the bench MAY  — every permissions: block in the file
#
# The model lives here, beside the error catalogue, for the same reason the
# catalogue does: the refusal a caller reads, the reference it can fetch, and the
# check that enforces the boundary all have to be one set of entries. A rights
# description maintained next to the enforcement is a description that will one
# day describe a boundary that is no longer there.
#
# Value shapes are NOT restated here. They are looked up in the shipped JSON
# schema by `config_key_schema`, so the only statement this module makes is the
# policy one: which key belongs to which right.
CONFIG_DESCRIPTION_RIGHT = "allow_config_description_write"
CONFIG_PERMISSIONS_RIGHT = "allow_config_permissions_write"
CONFIG_WRITE_RIGHT = "allow_config_write"

CONFIG_RIGHTS: dict[str, str] = {
    CONFIG_DESCRIPTION_RIGHT: (
        "Set the description of the bench field-wise: what hardware is there and how it is reached. "
        "Never a permissions: block, so it cannot widen what may be done to the hardware."
    ),
    CONFIG_PERMISSIONS_RIGHT: (
        "Set the permissions: blocks field-wise, including these two keys themselves. This is the grant "
        "that hands over the granting."
    ),
}


@dataclass(frozen=True)
class ConfigKeyRule:
    """One family of settable keys, and the right that opens it.

    ``fields`` empty means "every field the schema declares for this section
    except its permissions", which is how the decision phrases ``can_buses.<n>.*``
    and ``target.*``. Derived rather than listed, so a field added to the schema
    does not need a second edit here to become settable — and cannot be silently
    forgotten either.
    """

    section: str
    named: bool
    under_permissions: bool
    right: str
    fields: tuple[str, ...] = ()

    @property
    def pattern(self) -> str:
        entry = f"{self.section}.<name>" if self.named else self.section
        return f"{entry}.permissions.<flag>" if self.under_permissions else f"{entry}.<field>"


CONFIG_KEY_RULES: tuple[ConfigKeyRule, ...] = (
    # The description half. `target` and `can_buses` are whole sections in the
    # decision; `debuggers` and `com_ports` are the named subsets, because the
    # rest of those entries (type, flash_address, resource_id, timeouts, buffer
    # limits, DTR/RTS) changes what a call does to the board rather than what the
    # board is, and none of it is what an attached probe hands you.
    ConfigKeyRule("target", named=False, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT),
    ConfigKeyRule("debuggers", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("probe_id", "executable", "interface_cfg", "target_cfg")),
    ConfigKeyRule("com_ports", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("device", "baudrate")),
    ConfigKeyRule("can_buses", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT),
    # The permissions half, every block of it.
    ConfigKeyRule("permissions", named=False, under_permissions=False, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("debuggers", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("com_ports", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("can_buses", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
)

# Sections whose entries are named by the operator and may be added.
CONFIG_NAMED_SECTIONS = ("debuggers", "com_ports", "can_buses")


@dataclass(frozen=True)
class ResolvedConfigKey:
    """A dotted key resolved against the model above."""

    key: str
    section: str
    entry: str | None
    field: str
    under_permissions: bool
    right: str
    pattern: str

    @property
    def path(self) -> tuple[str, ...]:
        parts: list[str] = [self.section]
        if self.entry is not None:
            parts.append(self.entry)
        if self.under_permissions:
            parts.append("permissions")
        parts.append(self.field)
        return tuple(parts)


def _dereference(schema: JsonObject, node: object) -> JsonObject:
    if not isinstance(node, dict):
        return {}
    reference = node.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return node
    resolved: object = schema
    for part in reference[2:].split("/"):
        if not isinstance(resolved, dict):
            return {}
        resolved = resolved.get(part)
    return _dereference(schema, resolved)


def _section_entry_schema(schema: JsonObject, rule: ConfigKeyRule) -> JsonObject:
    """The schema node holding one entry's own properties."""
    section = _dereference(schema, (schema.get("properties") or {}).get(rule.section))
    if rule.named:
        section = _dereference(schema, section.get("additionalProperties"))
    if rule.under_permissions:
        section = _dereference(schema, (section.get("properties") or {}).get("permissions"))
    return section


@cache
def config_rule_fields(rule: ConfigKeyRule) -> tuple[str, ...]:
    """The field names this rule covers, read out of the shipped schema.

    An explicit subset is returned as written. A rule that names none covers
    everything the schema declares for that node except ``permissions`` (which
    belongs to the other right) and except keys the schema marks deprecated —
    ``allow_probe`` and ``allow_read`` exist only for version 1 files and are
    refused outright in a version 2 one."""
    if rule.fields:
        return rule.fields
    properties = _section_entry_schema(config_schema_document(), rule).get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(
        sorted(
            name
            for name, node in properties.items()
            if name != "permissions" and not (isinstance(node, dict) and node.get("deprecated") is True)
        )
    )


def config_key_schema(rule: ConfigKeyRule, field: str) -> JsonObject | None:
    """The shipped schema's own node for one settable key.

    This is the whole answer to "what may I put here": type, enum, pattern,
    minimum, default. Nothing about a value shape is written down twice, so the
    reference, the refusal and the check cannot drift from the file the loader
    validates against."""
    schema = config_schema_document()
    properties = _section_entry_schema(schema, rule).get("properties")
    if not isinstance(properties, dict) or field not in properties:
        return None
    # A copy, because this node travels into tool results and reference tables,
    # and the schema behind it is cached for the life of the process.
    return deepcopy(_dereference(schema, properties[field]))


def resolve_config_key(key: str) -> ResolvedConfigKey | None:
    """Resolve a dotted key, or None when nothing settable is spelled that way.

    Entry names may contain dots (the schema allows ``[A-Za-z0-9_.-]+``), so the
    key is read from the right: field names are a closed set and contain no dot,
    which makes the last component — or the last ``.permissions.<flag>`` pair —
    the only possible reading. ``debuggers.a.b.probe_id`` is therefore the entry
    named ``a.b``, unambiguously, and an entry named ``a.probe_id`` is still
    reachable as ``debuggers.a.probe_id.probe_id``."""
    for rule in CONFIG_KEY_RULES:
        prefix = f"{rule.section}."
        if not key.startswith(prefix):
            continue
        remainder = key[len(prefix) :]
        if rule.named:
            separator = ".permissions." if rule.under_permissions else "."
            entry, found, field = remainder.rpartition(separator)
            if not found or not entry:
                continue
        elif rule.under_permissions:  # pragma: no cover - no unnamed permissions block exists
            continue
        else:
            entry, field = None, remainder
        if field not in config_rule_fields(rule):
            continue
        return ResolvedConfigKey(key, rule.section, entry, field, rule.under_permissions, rule.right, rule.pattern)
    return None


def config_key_catalogue() -> list[JsonObject]:
    """Every settable key pattern, with its right and its value shape.

    One list, read by the reference document and by the rights-aware answer a
    caller gets from ``project_config_describe``."""
    catalogue: list[JsonObject] = []
    for rule in CONFIG_KEY_RULES:
        for field in config_rule_fields(rule):
            entry = f"{rule.section}.<name>" if rule.named else rule.section
            key = f"{entry}.permissions.{field}" if rule.under_permissions else f"{entry}.{field}"
            catalogue.append({"key": key, "right": rule.right, "value_schema": config_key_schema(rule, field) or {}})
    return catalogue


def config_permission_keys() -> tuple[str, ...]:
    """Every key that names a permission, by pattern.

    The complement of this set is the description half, and both halves come out
    of the one model above rather than out of two lists."""
    return tuple(str(entry["key"]) for entry in config_key_catalogue() if entry["right"] == CONFIG_PERMISSIONS_RIGHT)


# ---------------------------------------------------------------------------
# The shape of a configuration, as prose a caller can write from.
#
# `config-schema` already serves the shipped JSON Schema byte for byte. A schema
# says what is *valid*; it does not say what the sections are for, which of them
# a bench actually needs, or what a real one looks like filled in. Without that,
# the write path below is operable only by guessing — write, be refused, guess
# the next key — and every guess costs a round trip.
#
# So this document explains, and takes every value shape from the schema at read
# time. Two descriptions of one permission boundary drift, and the configuration
# is the permission boundary.

# What the schema cannot say about a section: why it is there. Keyed by section,
# merged with the schema's own description rather than replacing it.
_SECTION_PURPOSE: dict[str, str] = {
    "version": "Which permission model the file is read under. A new file should say `2`.",
    "workspace_root": "The project this configuration authorizes, and nothing else. A server started elsewhere refuses it.",
    "state_root": "Where leases, quarantine incidents and canonical reports live. Outside `workspace_root`, so repository content cannot forge them.",
    "permissions": "What may be done to this file itself.",
    "provenance": "Who wrote this file and who last changed it. A note to a reader; nothing reads it as policy.",
    "target": "What board this is. Names in reports; `controller` is what a human recognises.",
    "debuggers": "The debug probes. The entry name is the routing key a test plan addresses.",
    "debug": "Typed GDB session settings: which symbols may be read and how much.",
    "artifacts": "Which firmware files may be flashed, from where, and how large.",
    "com_ports": "The serial lines. Reading needs no permission; `assert_dtr`/`assert_rts` decide whether opening one restarts the target.",
    "can_buses": "The CAN buses. `listen_only` is how a bus is observed without sending ACK bits.",
    "validation": "How strictly a firmware artifact is checked before it is accepted.",
    "reports": "Where structured reports are written, relative to `state_root`.",
    "logs": "Where raw backend output is written, relative to `state_root`.",
    "recovery": "How far the owning process may clear its own hardware quarantine before an operator is required.",
}

CONFIG_WORKED_EXAMPLE = """version: 2

# Absolute, and this file is stored outside it.
workspace_root: "C:/Users/dana/work/thermostat-fw"
state_root: "C:/Users/dana/.agentic-hil/state"

permissions:
  # The agent may enter what it discovers about the bench …
  allow_config_description_write: true
  # … and may not grant itself anything.
  allow_config_permissions_write: false
  allow_config_write: false

target:
  name: "thermostat-dut"
  controller: "stm32f446re"

debuggers:
  dut:
    type: "openocd"
    executable: null            # found on PATH
    probe_id: "066AFF495451885087171450"
    interface_cfg: "interface/stlink.cfg"
    target_cfg: "target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      allow_raw_debugger_commands: false
      allow_mass_erase: false

com_ports:
  dut_uart:
    device: "COM7"              # the ST-Link virtual COM port
    baudrate: 115200
    assert_dtr: false           # this board wires DTR to reset
    assert_rts: false
    permissions:
      allow_write: true

can_buses: {}

artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
  allow_upload: false

recovery:
  auto_recover: "reset_halt"
  max_attempts: 3
"""


def _schema_type_label(node: JsonObject) -> str:
    declared = node.get("type")
    if isinstance(declared, list):
        label = " or ".join(str(item) for item in declared)
    elif isinstance(declared, str):
        label = declared
    else:
        label = "any"
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        label += " — one of " + ", ".join(f"`{json.dumps(item)}`" for item in enum)
    pattern = node.get("pattern")
    if isinstance(pattern, str):
        label += f", matching `{pattern}`"
    for bound in ("minimum", "maximum", "minLength"):
        if bound in node:
            label += f", {bound} {node[bound]}"
    return label


def _schema_field_rows(schema: JsonObject, node: JsonObject) -> list[str]:
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return []
    required = {str(item) for item in node.get("required", []) if isinstance(item, str)}
    rows: list[str] = []
    for name, raw in properties.items():
        field = _dereference(schema, raw)
        default = f"`{json.dumps(field['default'])}`" if "default" in field else ("**required**" if name in required else "—")
        description = str(field.get("description", "")).replace("\n", " ").replace("|", "\\|")
        if field.get("deprecated") is True:
            description = "**Deprecated.** " + description
        rows.append(f"| `{name}` | {_schema_type_label(field)} | {default} | {description} |")
    return rows


def _config_section_documents(schema: JsonObject) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):  # pragma: no cover - the shipped schema has properties
        return []
    required = {str(item) for item in schema.get("required", []) if isinstance(item, str)}
    blocks: list[str] = []
    for name, raw in properties.items():
        node = _dereference(schema, raw)
        purpose = _SECTION_PURPOSE.get(name, "")
        schema_description = str(node.get("description", "")).replace("\n", " ")
        heading = f"### `{name}`" + (" — required" if name in required else "")
        lines = [heading, ""]
        if purpose:
            lines.append(purpose)
        if schema_description and schema_description != purpose:
            lines.append("")
            lines.append(schema_description)
        entry_node = node
        if name in CONFIG_NAMED_SECTIONS:
            entry_node = _dereference(schema, node.get("additionalProperties"))
            lines += ["", f"A mapping of operator-chosen names to entries. Each `{name}.<name>` entry takes:"]
        rows = _schema_field_rows(schema, entry_node)
        if rows:
            lines += ["", "| Field | Value shape | Default | Meaning |", "|---|---|---|---|", *rows]
        elif name not in CONFIG_NAMED_SECTIONS:
            lines += ["", f"Value shape: {_schema_type_label(node)}."]
        blocks.append("\n".join(lines))
    return blocks


def _config_write_key_rows() -> list[str]:
    return [f"| `{entry['key']}` | `{entry['right']}` | {_schema_type_label(dict(entry['value_schema']))} |" for entry in config_key_catalogue()]


def config_shape_document() -> str:
    """The explanatory, rights-aware view of the configuration.

    Every value shape in it is read out of the shipped schema when this is
    called, so the two cannot disagree. What is written here is what a schema
    cannot carry: what a section is for, which ones a bench needs, a filled-in
    example, and how the file is changed over this connection."""
    schema = config_schema_document()
    sections = "\n\n".join(_config_section_documents(schema))
    keys = "\n".join(_config_write_key_rows())
    rights = "\n".join(f"| `permissions.{name}` | {purpose} |" for name, purpose in CONFIG_RIGHTS.items())
    return f"""# The shape of an Agentic HIL configuration, and how to change it

`{CONFIG_SCHEMA_URI}` serves the JSON Schema this file is validated against. A schema says what is **valid**; it does not say what a section is for, which ones a bench actually needs, or what a real one looks like filled in. This document is that, and it takes every value shape from the same schema at read time — there is no second list of types anywhere.

## Where the file is

One authoritative file per project, **outside** the workspace, so repository content can never rewrite policy. The server discovers it; `AGENTIC_HIL_CONFIG` may override the location with an absolute path. `{PLATFORM_PATHS_URI}` has the location rules.

`workspace_root` and `state_root` are the only two required keys. Everything else has a default, and a configuration that names only those two is valid — it just describes a bench with no hardware on it.

## The sections

{sections}

## A worked example

A Nucleo-F446RE on ST-Link, flashed through OpenOCD, talking over the probe's own virtual COM port. The operator has opened flashing and reset on this board, and has let the agent keep the bench description current without letting it grant itself anything:

```yaml
{CONFIG_WORKED_EXAMPLE}```

`probe_id` is the value nobody can guess and nobody enjoys transcribing: `debugger_probes_list` reads it off the attached probe, and `project_config_set` enters it.

## Changing it over MCP

Two calls, and they are the only door. The file itself is protected by deny rules `agentic-hil setup` writes into the host, so an agent's own file tools cannot touch it — that is the precondition for this door, not a contradiction of it.

| Call | Does |
|---|---|
| `project_config_describe` | answers, for **this** configuration in **this** state, which keys you may change right now, which you may not, and which grant would open a locked one. Needs no permission; reading is free. |
| `project_config_set` | sets named keys, field-wise, after checking each value against the schema. |
| `project_config_create` | regenerates the whole file from hardware discovery when `permissions.allow_config_write` is set. Contributes no permission value: the grants on disk are carried over unchanged. |

### The two rights

| Grant | Opens |
|---|---|
{rights}

The split is the point. Somebody who opens the file so an agent can enter a probe serial has not, in the same motion and without being told, handed over the ability for that agent to write `allow_flash: true` on itself.

### Which keys, and what may go in them

| Key | Grant that opens it | Value shape (from the schema) |
|---|---|---|
{keys}

`<name>` is an entry name you choose. A `debuggers`/`com_ports`/`can_buses` entry that does not exist yet is created by setting a key under it — always with every permission false, written by the server, so a new entry can never arrive pre-granted.

Entry names may contain dots. Keys are therefore read from the right: the field name is the last component, so `debuggers.a.b.probe_id` is the entry named `a.b`.

### The call

```json
{{"name": "project_config_set", "arguments": {{"changes": [
  {{"key": "debuggers.dut.probe_id", "value": "066AFF495451885087171450"}},
  {{"key": "com_ports.dut_uart.device", "value": "COM7"}}
]}}}}
```

Every change in one call is applied together or not at all. The changed document is validated on a temporary file before it replaces the real one, so a change that would make the configuration unloadable leaves the working file exactly as it was.

The write is recorded in `provenance`: `last_modified_by`, `last_modified_via`, `last_modified_at`, the keys that moved, and a running `modification_count`.

## What deliberately cannot be done

| Not possible | Why |
|---|---|
| writing the file with your own file tools | `setup` writes host deny rules against exactly that. One door, audited, locked by a grant inside the file. |
| sending a whole document, or a whole `debuggers.dut` subtree | the agent does not author this file. `value` accepts a string, number, boolean or null and nothing else, so no subtree — and no `permissions:` block inside one — can arrive as a value. |
| widening your own permissions with only the description grant | refused twice over: the key does not resolve to the description grant, and the permissions actually present in the document are compared before and after the change. A permission that changed without `{CONFIG_PERMISSIONS_RIGHT}` fails the write. |
| changing anything while a run is open | a run holds devices under the policy this file states. Changing the policy underneath it is refused; close the run with `bench_run_stop` first. |
| deleting a key, an entry, or the file | nothing on this surface removes configuration. Regenerating one costs an operator their settings. |
| `version`, `workspace_root`, `state_root`, `artifacts`, `debug`, `validation`, `recovery` | not settable over MCP at all. These decide where trusted state lives, which files may be flashed and how far the machine may recover itself. They are an operator's to write. |

A refused write names the grant that is missing and the key in this file that carries it. If the answer is `permission_denied`, that **is** the answer: report it and stop. The configuration belongs to the operator.

## Getting from "board attached" to a valid change

1. `project_config_describe` — what may this caller change right now.
2. `debugger_probes_list` / `com_ports_list` — what is actually attached.
3. `project_config_set` — enter it.
4. The server keeps serving the configuration it loaded at startup; the result says `reload_required`. Ask the operator to restart the MCP server before relying on the change.
"""


LEASE_LIFECYCLE_DOCUMENT = """# Device exclusivity, hardware leases, and the quarantine lifecycle

Two different things guard the hardware, and they live in two different places.

**Exclusivity** is machine-wide and keyed on the physical device. It is what replaced the read permission: reading needs no grant, because whoever holds a board cannot be disturbed and whoever reads while no run holds it disturbs nobody.

**The lease** is per configuration, lives under `state_root`, and records what a call did and whether the hardware was left in a confirmed state. It survives process exit; that is what makes an abandoned incident visible instead of forgotten.

## Device exclusivity

| Property | Rule |
|---|---|
| what is locked | the physical device: `physical:<resource_id>`, `probe:<identity>`, `com:<device>`, `can:<adapter>:<channel>` |
| case | a name for hardware — `resource_id`, a probe serial, a CAN channel — folds case on every platform, because `0669FF` and `0669ff` are one probe wherever the bench runs. A host path — a debugger executable, a serial device — folds the way its own filesystem does, so `COM7` and `com7` are one port on Windows while `/dev/ttyACM0` and `/dev/ttyacm0` are two on Linux. Two entries whose `resource_id` values differ only in case are refused at config load rather than merged |
| where | `~/.agentic-hil/device-locks`, one agreed place per machine, never under `state_root` — a lock kept per configuration is not a bench lock |
| how long | the whole run, from the declaration to its end; the lease each call takes borrows that hold |
| what may be touched | only what the test description declares; anything else is refused with `undeclared_device` |
| contention | refused with `device_busy`, naming the holder; waiting happens only when the caller asked for it with `wait_s`, and stays bounded |
| a crashed owner | the operating system drops the lock when the process dies, so the device is free immediately — no quarantine, no `recover`, no waiting |

A call outside a declared run still takes the device for the length of the call, so a lone observation is refused while a run holds the board and works when none does.

## Declaring a run over MCP

A single call needs no declaration. A *sequence* does: without one, `flash_firmware`, `reset_target` and `com_read` are three runs, each holding its device only for its own duration, and between them the board is free for anything else on this machine.

```json
{"name": "bench_run_start", "arguments": {
  "devices": [{"kind": "debugger", "id": "dut"}, {"kind": "uart", "id": "dut_uart"}],
  "label": "boot-smoke"
}}
```

| Tool | Does |
|---|---|
| `bench_run_start` | resolves every named device, then takes the whole set at once; holds it until the run ends |
| `bench_run_stop` | releases the run's devices; safe to call when no run is open |
| `bench_run_status` | whether a run is open here, what it declared, and since when |

`kind` is one of `debugger`, `uart`, `can`. `id` is the name of the config entry; for `debugger` it may be omitted when the project configures exactly one. The DUT is not a kind: it is what the devices drive, not something that drives.

What the declaration buys, and what it costs:

- **Held for the run.** Every call inside the run borrows the run's hold instead of taking its own, so no gap opens between two steps.
- **Nothing else may be touched.** A call reaching for a device the run did not declare is refused with `undeclared_device`. Declare everything the sequence touches, up front.
- **All or nothing.** A declared device already held fails `bench_run_start` immediately with `device_busy` naming the holder. Nothing is left half-acquired: if the third of four is busy, the first two were already given back before the refusal returned.
- **Fixed order.** Devices are taken sorted by lock key, never in the order they were declared, so two runs reaching for an overlapping set cannot each hold what the other needs next.

### If a run is never closed

Nothing times a run out, and that is deliberate: dropping a device that may be mid-operation is exactly the silent failure exclusivity exists to prevent. A run therefore holds its devices until one of these happens.

| Event | What frees the devices |
|---|---|
| `bench_run_stop` | the run releases them itself |
| the client disconnects | stdin reaches EOF, the server shuts its service down, and an open run is released on the way out |
| the server process dies | the operating system drops the advisory lock it held; the next owner takes the device and its result carries `reclaimed` with reason `owner_process_exited_without_release` |

So an abandoned run costs nothing beyond the life of the server process. While it lasts, a contender's `device_busy` refusal carries `heartbeat_age_s` and, past four heartbeat intervals, `holder_heartbeat_stale: true` — an idle holder is visible rather than merely obstructive. Call `bench_run_status` if you are unsure whether you still hold the bench, and `bench_run_stop` to be sure you do not.

One case is outside this: a server process left running with its stdin never closed, by a host that leaked the pipe. It sees no disconnect and holds the run. Ending that process is the answer; never delete a lock file under `~/.agentic-hil/device-locks`.

### Two config entries, one board

The lock is keyed on the hardware, not on the name of the config entry. Two entries that describe one physical unit — a debug probe and its virtual COM port sharing a `resource_id`, or the same serial device configured twice — resolve to one lock key and are taken once. Declaring both is not an error and does not double-lock anything.

The one identity that is *not* hardware-derived: a debugger entry with neither `resource_id` nor `probe_id` falls back to the backend toolchain. Two boards driven by the same backend would then share one lock, and one board reached through two backends would take two. `bench_run_start` returns a `warnings` entry when a declared device is in that state; the fix is a `probe_id`, or a `resource_id` shared by every entry naming that unit.

Reading can still perturb a target — an SWD attach halts the core, a CAN controller outside `listen_only` sends dominant ACK bits, opening a serial port raises DTR on boards that wire it to reset. That is why the passive modes stay available: `can_buses.<name>.listen_only: true` and `com_ports.<name>.assert_dtr: false` / `assert_rts: false` are how a target is observed provably undisturbed. They are no longer a precondition for access; they are the way to prove a reading did not touch anything.

## Lease states

`lease_state` appears in every hardware result.

| lease_state | Meaning | Continue? |
|---|---|---|
| `null` | the call took no lease | yes |
| `active` | held by this owner | yes |
| `released` | returned cleanly | yes |
| `cleanup_required` | released without a confirmed safe state | no |
| `quarantined` | blocked for this project until recovery | no |
| `stale` | the owner is gone and the state was not settled | no |

## Continue only when all of these hold

```text
ok == true
target_ok      != false
audit_ok       != false
cleanup_ok     != false
cleanup_required != true
quarantined      != true
lease_state      in {null, active, released}
side_effect_status not in {unknown, partial}
hardware_state     != unknown
```

`agentic_hil.report.overall_success()` is exactly this predicate. Do not re-derive it from `ok` alone.

## When a call is refused with `resource_quarantined`

1. Stop every effect. Do not retry around it, and never delete state under `state_root`.
2. Read `hardware_lease_status` (CLI: `agentic-hil lease-status`). It reports `cleanup_reasons`, `auto_recoverable`, `auto_recover_policy`, and the current `quarantine_id`.
3. Branch on `auto_recoverable`.

| Field | Value | Do |
|---|---|---|
| `auto_recoverable` | `true` | Retry the hardware call **once**. The running owner reaps its leftover debugger processes, verifies the safe state under the bench policy, and proceeds. |
| `auto_recovery_attempted` in the refusal | `true` | That already ran and did not confirm the safe state. Do not retry again; go to the operator path. |
| `auto_recoverable` | `false` | Operator path. |

## Operator path

The operator physically confirms the bench, then:

```bash
agentic-hil lease-status
agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>
```

Both flags are mandatory. `--quarantine-id` must be the id `lease-status` reports right now; a stale id returns `quarantine_changed`, which means the incident moved and has to be re-read.

### `--accept-config-change`

`recover` compares the SHA-256 of the authoritative configuration against the one recorded when the incident was raised. If they differ it refuses with:

```json
{"error_type": "config_changed", "recorded_config_sha256": "...", "current_config_sha256": "..."}
```

The recorded config is what defined the resources, permissions, and limits under which the incident happened; recovering against a different one would clear an incident nobody assessed. The operator reviews the delta between the two configs and reruns with `--accept-config-change` to state explicitly that the change is understood and accepted. The override is recorded in the recovery audit log as `config_change_accepted`.

## `recovery.auto_recover` policy

Set in the authoritative configuration; decides how far the owning process may go on its own.

| Value | Owner may | Use when |
|---|---|---|
| `off` | nothing; only an operator clears a quarantine | any bench where a machine decision is unacceptable |
| `readonly` | reap this owner's debugger processes and re-read the probe; nothing physical | peripherals react to a reset |
| `reset_halt` (default) | additionally drive a reset-into-halt to settle an unconfirmed flash, reset, or session start | default bench |

`reset_halt` degrades to `readonly` when the bound debugger lacks `allow_reset`. `recovery.max_attempts` (default 3, range 1-10) caps machine attempts per incident before the owner defers to an operator.

## Recovery outcomes

| error_type | Meaning |
|---|---|
| `operator_confirmation_required` | `--confirm-safe-state` was not given |
| `quarantine_id_required` | `--quarantine-id` was not given |
| `resource_not_quarantined` | nothing to recover |
| `quarantine_changed` | the incident or a resource marker moved; re-read `lease-status` |
| `config_changed` | see `--accept-config-change` above |
| `resource_busy` | a live owner still holds project resources; stop it first |
| `recovery_audit_failed` | the audit could not be persisted, so the quarantine stands |
"""


PLATFORM_PATHS_DOCUMENT = """# Path trust rules and where files may live

Every configured path is checked before it is used, so that no other local principal can replace the configuration that grants hardware permissions, or the state that records leases and quarantines. The validator only reads ACLs and modes. It never changes them, and neither should you.

## What is checked

| Scope | Windows | POSIX |
|---|---|---|
| final directory, owner | current user, `SYSTEM`, or `BUILTIN\\Administrators` | current euid |
| final directory, rights | no other principal holds `0x500D0116` (write, delete, WRITE_DAC, WRITE_OWNER) | not group- or other-writable |
| every ancestor | no other principal holds `0x100D0040` (DELETE, DELETE_CHILD, WRITE_DAC, WRITE_OWNER); inherit-only ACEs are excluded | not group- or other-writable unless sticky (`01777`) |

A failure raises `error_type: unsafe_configured_path` and names the offending component in `path` and the setting in `field`. On Windows it also names *who* holds the right, in `untrusted_principals`.

## Windows 11: measured verdicts on an affected profile

| Path | Verdict | Reason |
|---|---|---|
| `C:\\` | pass | `SYSTEM`, `Administrators` |
| `C:\\Users` | pass | `SYSTEM`, `Administrators` |
| `C:\\Users\\<user>` | pass | `SYSTEM`, `Administrators`, `<user>` |
| `C:\\Users\\<user>\\AppData` | **fail** | app-capability ACE `S-1-15-3-*` with FullControl |
| `%APPDATA%` = `...\\AppData\\Roaming` | **fail** | inherits the AppData ACE |
| `%LOCALAPPDATA%` = `...\\AppData\\Local` | **fail** | inherits the AppData ACE |
| `C:\\Users\\<user>\\.agentic-hil` | pass | no ancestor below the profile root |
| `C:\\Users\\Default\\AppData` | pass | no capability ACE at all |

`S-1-15-3-*` is an app-capability SID belonging to a packaged application. It is none of the three trusted principals and it holds FullControl, so the ancestor check fails there. Two ACEs of that SID sit on `AppData`: one inherit-only, which the check correctly skips, and one effective on the directory itself, which really does carry DELETE and WRITE_DAC. The refusal is not a false positive.

It is also not a Windows default. `C:\\Users\\Default` is the template every new profile is copied from and it carries no such ACE, so the grant arrives later, with an installed application. Whether your profile is affected is therefore a property of what is installed on it.

Consequence where it is present: the built-in defaults `%APPDATA%\\agentic-hil` (configuration) and `%LOCALAPPDATA%\\agentic-hil` (state) are rejected, and every config load hits it — as does `agentic-hil init`, which writes them.

## The refusal names the holder

An `unsafe_configured_path` refusal on Windows carries `untrusted_principals`, one entry per principal that holds the rejected right:

```json
{
  "error_type": "unsafe_configured_path",
  "field": "user_config",
  "path": "C:\\\\Users\\\\<user>\\\\AppData\\\\Roaming",
  "untrusted_principals": [
    {
      "sid": "S-1-15-3-3557520199-...",
      "kind": "app_capability",
      "package_sid": "S-1-15-2-3557520199-...",
      "package_family": "<name>_<publisher-id>",
      "display_name": "<application>",
      "package": "<Name>_<version>_<arch>__<publisher-id>"
    }
  ]
}
```

A capability SID `S-1-15-3-<sub-authorities>` shares its sub-authorities with the package SID `S-1-15-2-<the same sub-authorities>`, which Windows registers under `HKLM\\SOFTWARE\\Microsoft\\SecurityManager\\CapAuthz\\ApplicationsEx\\<PackageFullName>\\PackageSid` and under `HKEY_CLASSES_ROOT\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\AppContainer\\Mappings\\<package SID>`. Both are readable without privileges.

Resolution is best effort. A SID that resolves to nothing is still reported as `sid`, and the refusal is unchanged: the right exists whether or not anyone can put a name to it. The entry never carries a name that was guessed.

Knowing the package turns "an unknown principal has rights here" into a decision the operator can actually take: use a permitted location, or remove that application's grant. Agentic HIL does neither on its own — it reads ACLs and never writes them.

## The override is the supported answer, not a workaround

`AGENTIC_HIL_CONFIG` and a freely chosen `state_root` are not debug switches. They are how a project binds to a configuration and a state directory that the discovered defaults do not cover, and a default location the trust check refuses is exactly that case.

```text
# Windows, on a profile where %APPDATA% is refused
AGENTIC_HIL_CONFIG=C:\\Users\\<user>\\.agentic-hil\\projects\\<workspace-name>\\config.yaml

# inside that config.yaml
state_root: C:\\Users\\<user>\\.agentic-hil\\state
```

Using them costs nothing else: `state_root` has no fixed location beyond being absolute, non-overlapping with `workspace_root`, and passing the check, and a configuration selected by `AGENTIC_HIL_CONFIG` is read exactly like a discovered one — same schema, same validation, same permissions. Nothing about a project is second class for having taken this route.

The one rule that does not bend: set `AGENTIC_HIL_CONFIG` in the host's user-level, managed, or parent-process environment, never in a repository-controlled file. An agent that can edit the file that selects the configuration can select a configuration it wrote.

## Which command touches which of these

Only the project half touches the paths above. User scope is per user, per machine: all of it under the invoking user's home, invisible to other OS users on the host.

| Command | Scope | Touches | On a profile that rejects the paths above |
|---|---|---|---|
| `agentic-hil agent-install --agent <agent>` | user, once per user and agent | the agent's skill directory and its user-level MCP config, both under the home directory; checks that a persistent trusted executable exists | completes; reads and writes no configuration and no `state_root` |
| `agentic-hil init [--agent <agent>]` | project, once per workspace | the authoritative configuration and `state_root`, then `doctor` | refuses; those are exactly the rejected paths |
| `agentic-hil setup --agent <agent>` | both, in order | both of the above | the user half completes and stays; the project half refuses and rolls back only itself |

A rejected configuration location therefore leaves the agent installed and working. Fix the location as below, then run `agentic-hil init` alone; `agent-install` need not run again for this user, on this project or any other.

## Where to put things instead

| Item | Windows | POSIX |
|---|---|---|
| authoritative configuration | `%USERPROFILE%\\.agentic-hil\\projects\\<workspace-name>\\config.yaml`, selected by `AGENTIC_HIL_CONFIG` | default `$XDG_CONFIG_HOME/agentic-hil/projects/<name>-<digest>/config.yaml` passes |
| `state_root` | `%USERPROFILE%\\.agentic-hil\\state` | default `$XDG_STATE_HOME/agentic-hil` passes |
| device locks | `%USERPROFILE%\\.agentic-hil\\device-locks`, fixed | `~/.agentic-hil/device-locks`, fixed |

The device lock directory is not configurable and has no environment override. It is the one place every process on this machine agrees to look for who holds a board, and an override is how two sessions stop seeing each other — which is the failure it exists to prevent. The home directory is chosen because it is the only location whose ancestors pass this check on a stock profile of either platform: a directory shared across *users* (`C:\\ProgramData`, `/var/lib`) is either writable by principals the check rejects or needs root to create, so exclusivity reaches every process of one user rather than every user of one machine.

Rules that hold on both platforms:

- `AGENTIC_HIL_CONFIG` is optional and must be an absolute path to the configuration file.
- Set `AGENTIC_HIL_CONFIG` in the host's user-level, managed, or parent-process environment. Never in a repository-controlled file (`.vscode/mcp.json`, `.mcp.json`, `.codex/config.toml`, `opencode.json`).
- `workspace_root` and `state_root` are both mandatory and absolute, and must not overlap in either direction.
- The discovered default configuration path is derived from the workspace path, so it is canonical per workspace; a config found elsewhere is only accepted through `AGENTIC_HIL_CONFIG`.
- Whether an agent may write the configuration is decided by the configuration, in `permissions.allow_config_write`, and by nothing else. There is no second state store: what holds is what a person reads in the file. A workspace with no configuration lets an agent generate one, and a configuration deleted out of band lets it generate a fresh one — the deny-by-default skeleton again, so the round trip costs a human their customised permissions and gains an agent nothing.

## Do not

Do not relax an ACL, take ownership, or grant yourself rights on a rejected path or any ancestor to make the check pass. The ancestor is rejected precisely because a second principal can replace it; changing that removes the guarantee rather than meeting it. On `AppData` it also does not hold: the application that made the grant re-applies it.

Removing the grant is the operator's call, not the tool's, and it is made through the application that holds it — not by editing the ACL underneath it. Moving the configuration and `state_root` needs no privileges, changes nothing on the system, and is the answer this project supports.
"""


TARGET_SUPPORT_DOCUMENT = """# Selecting the target: which field, which value, where it comes from

Which field names the target depends on the backend. Setting the wrong one is silent, because each backend ignores the fields it does not read.

| Backend | Field that names the target | Values come from |
|---|---|---|
| `openocd` | `target_cfg` (plus `interface_cfg`) | OpenOCD's bundled scripts, e.g. `target/stm32f4x.cfg`, `interface/stlink.cfg` |
| `stlink` | none; STM32CubeProgrammer identifies the part itself | not applicable |
| `pyocd` | `target_type` | pyOCD's built-in list plus every installed CMSIS device-family pack |

## Known-good values, STM32 Nucleo-F446RE with the on-board ST-Link

```yaml
# openocd
interface_cfg: interface/stlink.cfg
target_cfg: target/stm32f4x.cfg

# pyocd
target_type: stm32f446retx   # also accepted: stm32f446re

# stlink
interface: SWD
```

`flash_address: "0x08000000"` is required only to flash a `.bin` on `stlink` or `pyocd`.

## pyOCD target types mostly come from CMSIS packs

Most vendor parts, including the whole STM32F4 family, are not built into pyOCD. They exist only after a device-family pack is installed, and `pyocd list --targets` says so in its Source column:

```text
$ pyocd list --targets
  Name             Vendor              Part Number     Families                    Source
  stm32f446re      STMicroelectronics  STM32F446RE     STM32F4 Series, STM32F446   pack
  stm32f446retx    STMicroelectronics  STM32F446RETx   STM32F4 Series, STM32F446   pack
```

Without the pack the same value is simply unknown. pyOCD refuses with `Target type <name> not recognized`, which surfaces as `error_type: target_type_invalid` with `backend_error_type` from pyOCD and the install command in `install_commands`.

Install it as a deliberate host setup step, not in passing:

```bash
pyocd pack find stm32f446        # what packs offer this part; GLOB, so shorten to widen
pyocd pack install stm32f446retx # downloads the pack from the vendor index
pyocd pack show                  # what is installed now
```

## Nothing downloads a pack for you

`pyocd pack install` fetches a device-family pack over the network from the vendor index. Agentic HIL never runs it, never runs it for you as part of another call, and has no setting that would. It names the command; a person runs it, knowing what is being fetched and from where.

That rule exists because of what happened without it: an agent looking for the right `target_type` found nothing about packs anywhere, escalated through pyOCD's installed sources and `cmsis_pack_manager`'s internals, and ended up fetching a `.pdsc` from a vendor site by hand — twice — without anyone confirming the download. Do not reconstruct a pack from hand-downloaded `.pdsc` files. `pyocd pack install` is the one supported route.

## Where installed packs live

Two locations exist and they are not the same one.

| What | Where | Set by |
|---|---|---|
| packs installed by `pyocd pack install` | `cmsis-pack-manager`'s data directory: `%LOCALAPPDATA%\\cmsis-pack-manager\\cmsis-pack-manager` on Windows, `~/.local/share/cmsis-pack-manager` on Linux, `~/Library/Application Support/cmsis-pack-manager` on macOS | not configurable through an environment variable; `pyocd pack show` reports what is there |
| a CMSIS-Toolbox pack root | `CMSIS_PACK_ROOT`, defaulting to `%LOCALAPPDATA%\\Arm\\Packs` on Windows and `~/.cache/arm/packs` on POSIX | read by pyOCD only for its `cbuild-run` support |

So `CMSIS_PACK_ROOT` does **not** relocate the cache `pyocd pack install` writes to. A pack outside either location is passed explicitly with pyOCD's `pack` session option, which Agentic HIL does not configure.

These are host setup commands for the toolchain, not hardware actions. They do not replace the Agentic HIL tools: probing, flashing, resetting, and reading the target still go through the tools, never through `pyocd`, `openocd`, `st-flash`, or a Makefile target that calls one.

## `doctor` asks before the flash does

`agentic-hil doctor` reports `debuggers.<name>.target_support` for every checked debugger, so a missing pack is found at setup rather than at the first flash.

| `status` | Means | `doctor` |
|---|---|---|
| `supported` | the backend resolves the configured `target_type`; `source` says `builtin` or `pack` | green |
| `unsupported` | the backend enumerated its target types and this one is not among them, so no flash can work | **red**, with `install_commands` |
| `undetermined` | this host could not answer — no toolchain installed, the enumeration failed or could not be read | green, and `undetermined_reason` says why |
| `not_configured` | no `target_type` is set, so pyOCD would guess from the probe's board ID | green |
| `not_applicable` | this backend has no target type: OpenOCD uses `target_cfg`, STM32CubeProgrammer identifies the part itself | green |

The line between `unsupported` and `undetermined` is deliberate. `unsupported` is a fact about this host; `undetermined` says nothing about the configuration and must not be read as one. A bench with no debugger toolchain installed yet stays green — `agentic-hil setup` rolls back on a red `doctor`, so conflating the two would break installation on exactly the fresh machine the check is meant to help.

`undetermined` is also not a pass. It means the question was asked and went unanswered, and the `doctor` summary says so.

## A green run can depend on a pack nobody recorded

If `target_type` resolves on one machine and not on another, the difference is the installed pack, not the configuration. `pyocd pack show` reports what the working machine actually has.
"""


def _resource_descriptor(uri: str, name: str, title: str, description: str, mime_type: str) -> JsonObject:
    return {"uri": uri, "name": name, "title": title, "description": description, "mimeType": mime_type}


MCP_RESOURCES: list[JsonObject] = [
    _resource_descriptor(
        DEBUGGER_BACKENDS_URI,
        "debugger-backends",
        "Required fields per debugger backend",
        "Which of type, executable, probe_id, target_type, interface, interface_cfg, target_cfg and flash_address each of openocd, stlink and pyocd requires, discovers, or ignores; when probe_id becomes mandatory; when flash_address is needed.",
        JSON_MIME,
    ),
    _resource_descriptor(
        ERRORS_URI,
        "errors",
        "Error types with remediation",
        "Every error_type that carries a concrete fix, with its meaning, ordered remediation steps, and the wrong fix to avoid. Identical to the remediation a failing result carries inline.",
        JSON_MIME,
    ),
    _resource_descriptor(
        CONFIG_SCHEMA_URI,
        "config-schema",
        "Authoritative configuration JSON Schema",
        "The bundled JSON Schema for the authoritative project configuration: every field, type, enum, default, and per-device permission.",
        JSON_MIME,
    ),
    _resource_descriptor(
        CONFIG_SHAPE_URI,
        "config-shape",
        "What a configuration looks like, and how to change it over MCP",
        "Which sections a configuration has and what each is for, which are required, and a worked Nucleo-F446RE example; then how to change one field-wise over MCP, the two grants that open the description and the permissions halves, which key each grant opens, and what deliberately cannot be done. Value shapes are read from the shipped schema, not restated.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        LEASE_LIFECYCLE_URI,
        "lease-lifecycle",
        "Device exclusivity, lease and quarantine lifecycle",
        "Which device a run locks and for how long, what device_busy and undeclared_device mean, why a crashed run needs no recovery; then lease states, the predicate that decides whether to continue, the auto-recovery branch, and the operator recovery path including --confirm-safe-state, --quarantine-id and --accept-config-change.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        PLATFORM_PATHS_URI,
        "platform-paths",
        "Path trust rules and permitted locations",
        "What the ancestor check verifies, which Windows and POSIX locations pass it and why, how a refusal names the Windows package that holds the right, why AGENTIC_HIL_CONFIG and a chosen state_root are the supported answer to a refused location, and why changing an ACL is the wrong fix.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        TARGET_SUPPORT_URI,
        "target-support",
        "Target selection and target support",
        "Which field names the target per backend, known-good values for the Nucleo-F446RE, how pyOCD target types are provided by CMSIS packs, how to find and install the right one and where installed packs live, and what doctor's target_support statuses mean — including why 'undetermined' is not a failure.",
        MARKDOWN_MIME,
    ),
]

MCP_RESOURCE_TEMPLATES: list[JsonObject] = [
    {
        "uriTemplate": ERROR_URI_PREFIX + "{error_type}",
        "name": "error",
        "title": "Remediation for one error type",
        "description": "The catalogue entry for a single error_type, e.g. agentic-hil://reference/errors/unsafe_configured_path. Append ':<scope>' for a backend- or field-specific entry, e.g. .../target_not_detected:pyocd.",
        "mimeType": JSON_MIME,
    },
    {
        "uriTemplate": DEBUGGER_BACKEND_URI_PREFIX + "{backend}",
        "name": "debugger-backend",
        "title": "Required fields for one debugger backend",
        "description": "The field matrix for a single backend: openocd, stlink, or pyocd.",
        "mimeType": JSON_MIME,
    },
]


def _json_content(uri: str, payload: JsonObject) -> JsonObject:
    # These reference documents are declared application/json and are read by an
    # agent, which parses them; the Markdown resources are the ones written to be
    # read. Serialize compactly for the same reason the tool result text block is.
    return {"uri": uri, "mimeType": JSON_MIME, "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}


def _markdown_content(uri: str, text: str) -> JsonObject:
    return {"uri": uri, "mimeType": MARKDOWN_MIME, "text": text}


def read_resource(uri: str) -> JsonObject | None:
    """The contents entry for a resource URI, or None when nothing serves it."""
    if uri == DEBUGGER_BACKENDS_URI:
        return _json_content(uri, debugger_backends_document())
    if uri == ERRORS_URI:
        return _json_content(uri, errors_document())
    if uri == CONFIG_SCHEMA_URI:
        return {"uri": uri, "mimeType": JSON_MIME, "text": config_schema_text()}
    if uri == CONFIG_SHAPE_URI:
        return _markdown_content(uri, config_shape_document())
    if uri == LEASE_LIFECYCLE_URI:
        return _markdown_content(uri, LEASE_LIFECYCLE_DOCUMENT)
    if uri == PLATFORM_PATHS_URI:
        return _markdown_content(uri, PLATFORM_PATHS_DOCUMENT)
    if uri == TARGET_SUPPORT_URI:
        return _markdown_content(uri, TARGET_SUPPORT_DOCUMENT)
    if uri.startswith(ERROR_URI_PREFIX):
        entry = catalogue_entry(uri[len(ERROR_URI_PREFIX) :])
        return None if entry is None else _json_content(uri, entry)
    if uri.startswith(DEBUGGER_BACKEND_URI_PREFIX):
        backend = uri[len(DEBUGGER_BACKEND_URI_PREFIX) :]
        matrix = DEBUGGER_FIELD_MATRIX.get(backend)
        if matrix is None:
            return None
        return _json_content(
            uri,
            {
                "backend": backend,
                "config_path": "debuggers.<name>.<field>",
                "fields": matrix,
                "probe_id_when_multiple_probes": MULTI_PROBE_RULE,
                "flash_address": FLASH_ADDRESS_RULE,
            },
        )
    return None
