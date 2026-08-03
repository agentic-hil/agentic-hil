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
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agentic_hil.types import JsonObject

RESOURCE_SCHEME = "agentic-hil"

CONFIG_SCHEMA_URI = f"{RESOURCE_SCHEME}://reference/config-schema"
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

    Windows applies an app-capability ACE (``S-1-15-3-*``, FullControl) to
    ``%USERPROFILE%\\AppData`` and inherits it into ``Roaming`` and ``Local``, so
    both fail the ancestor check while the profile root itself passes.
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
    "unsafe_configured_path": ErrorRemedy(
        meaning=(
            "A configured path, or one of its ancestor directories, can be replaced or re-permissioned by a principal "
            "other than the current user, SYSTEM, or Administrators. The validator only reads ACLs; it never changes them."
        ),
        remediation=(
            "Move the path under {safe_user_root}, whose every ancestor passes the check on a stock profile.",
            "Windows: %USERPROFILE% passes; %APPDATA% and %LOCALAPPDATA% do not, because AppData carries an "
            "app-capability ACE (S-1-15-3-*, FullControl) that both inherit.",
            "Re-run the command that failed. Nothing else has to change.",
            "Full rules and the measured per-directory verdicts: MCP resource " + PLATFORM_PATHS_URI + ".",
        ),
        do_not=(
            "Do not change the ACL or the owner of the rejected path or any ancestor. That removes the protection the "
            "check exists for instead of satisfying it, and on AppData Windows re-applies the ACE anyway.",
        ),
    ),
    "unsafe_configured_path:user_config": ErrorRemedy(
        meaning=(
            "The directory or file holding the authoritative configuration has an untrusted ancestor. On a stock "
            "Windows 11 profile the documented default under %APPDATA% is exactly this case."
        ),
        remediation=(
            "Create {safe_user_root}/projects/<workspace-name>/ and move config.yaml there.",
            "Point the server at it with an absolute path: AGENTIC_HIL_CONFIG={safe_user_config}",
            "Set AGENTIC_HIL_CONFIG in the host's user-level or managed environment, never in a repository-controlled "
            "file such as .vscode/mcp.json, .mcp.json, .codex/config.toml, or opencode.json.",
            "Re-run `agentic-hil doctor` to confirm the new location loads.",
        ),
        do_not=(
            "Do not change the ACL or the owner of %APPDATA%, %LOCALAPPDATA%, or AppData itself. Windows sets that ACE "
            "for packaged applications and re-applies it; relaxing it weakens every application in the profile.",
        ),
    ),
    "unsafe_configured_path:state_root": ErrorRemedy(
        meaning=(
            "state_root, one of its ancestors, or a directory derived from it failed the trust check. state_root holds "
            "hardware leases, quarantine incidents, and canonical reports, so a replaceable ancestor would let another "
            "local principal forge them."
        ),
        remediation=(
            "Set `state_root: {safe_state_root}` in the authoritative configuration.",
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
    "target_type_invalid:pyocd": ErrorRemedy(
        meaning=(
            "pyOCD does not resolve `debuggers.<name>.target_type`. Most vendor parts are not built into pyOCD; they "
            "come from an installed CMSIS device-family pack."
        ),
        remediation=(
            "Check the spelling against `pyocd list --targets`; the Source column says `builtin` or `pack`.",
            "If the part is not listed, install its pack as a deliberate host setup step: `pyocd pack find <part>` then "
            "`pyocd pack install <target_type>`. This downloads from the vendor index.",
            "Re-run the failing tool afterwards. Provenance and the location of the pack cache: MCP resource "
            + TARGET_SUPPORT_URI
            + ".",
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


LEASE_LIFECYCLE_DOCUMENT = """# Device exclusivity, hardware leases, and the quarantine lifecycle

Two different things guard the hardware, and they live in two different places.

**Exclusivity** is machine-wide and keyed on the physical device. It is what replaced the read permission: reading needs no grant, because whoever holds a board cannot be disturbed and whoever reads while no run holds it disturbs nobody.

**The lease** is per configuration, lives under `state_root`, and records what a call did and whether the hardware was left in a confirmed state. It survives process exit; that is what makes an abandoned incident visible instead of forgotten.

## Device exclusivity

| Property | Rule |
|---|---|
| what is locked | the physical device: `physical:<resource_id>`, `probe:<identity>`, `com:<device>`, `can:<adapter>:<channel>` |
| where | `~/.agentic-hil/device-locks`, one agreed place per machine, never under `state_root` — a lock kept per configuration is not a bench lock |
| how long | the whole run, from the declaration to its end; the lease each call takes borrows that hold |
| what may be touched | only what the test description declares; anything else is refused with `undeclared_device` |
| contention | refused with `device_busy`, naming the holder; waiting happens only when the caller asked for it with `wait_s`, and stays bounded |
| a crashed owner | the operating system drops the lock when the process dies, so the device is free immediately — no quarantine, no `recover`, no waiting |

A run outside a declared run still takes the device for the length of the call, so a lone observation is refused while a run holds the board and works when none does.

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

A failure raises `error_type: unsafe_configured_path` and names the offending component in `path` and the setting in `field`.

## Windows 11, stock user profile: measured verdicts

| Path | Verdict | Reason |
|---|---|---|
| `C:\\` | pass | `SYSTEM`, `Administrators` |
| `C:\\Users` | pass | `SYSTEM`, `Administrators` |
| `C:\\Users\\<user>` | pass | `SYSTEM`, `Administrators`, `<user>` |
| `C:\\Users\\<user>\\AppData` | **fail** | app-capability ACE `S-1-15-3-*` with FullControl |
| `%APPDATA%` = `...\\AppData\\Roaming` | **fail** | inherits the AppData ACE |
| `%LOCALAPPDATA%` = `...\\AppData\\Local` | **fail** | inherits the AppData ACE |
| `C:\\Users\\<user>\\.agentic-hil` | pass | no ancestor below the profile root |

`S-1-15-3-*` is an app-capability SID that Windows itself applies to `AppData` for packaged applications. It is none of the three trusted principals and it holds FullControl, so the ancestor check fails there. This is the check working as specified, not a bug in the profile.

Consequence: on Windows the built-in defaults `%APPDATA%\\agentic-hil` (configuration) and `%LOCALAPPDATA%\\agentic-hil` (state) are rejected, and every config load hits it — as does `agentic-hil init`, which writes them.

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

## Do not

Do not relax an ACL, take ownership, or grant yourself rights on a rejected path or any ancestor to make the check pass. The ancestor is rejected precisely because a second principal can replace it; changing that removes the guarantee rather than meeting it. On `AppData` it also does not work: Windows re-applies the inherited ACE.
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

Without the pack the same value is simply unknown, and the failure surfaces as `error_type: target_type_invalid` with `backend_error_type` from pyOCD.

Install it as a deliberate host setup step, not in passing:

```bash
pyocd pack find stm32f446        # what packs offer this part
pyocd pack install stm32f446retx # downloads the pack from the vendor index
pyocd pack show                  # what is installed now
```

`pyocd pack install` fetches from the network. Run it knowingly; do not fabricate an equivalent by downloading `.pdsc` files by hand. The pack cache location is pyOCD's, overridable with the `CMSIS_PACK_ROOT` environment variable.

These are host setup commands for the toolchain, not hardware actions. They do not replace the Agentic HIL tools: probing, flashing, resetting, and reading the target still go through the tools, never through `pyocd`, `openocd`, `st-flash`, or a Makefile target that calls one.

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
        "What the ancestor check verifies, which Windows and POSIX locations pass it and why, where the configuration and state_root belong, and why changing an ACL is the wrong fix.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        TARGET_SUPPORT_URI,
        "target-support",
        "Target selection and target support",
        "Which field names the target per backend, known-good values for the Nucleo-F446RE, and how pyOCD target types are provided by CMSIS packs.",
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
    return {"uri": uri, "mimeType": JSON_MIME, "text": json.dumps(payload, indent=2, ensure_ascii=False)}


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
