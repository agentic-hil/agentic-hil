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

# The loaded configuration and the file it came from have come apart. Not a
# refusal — every tool still works — but it is carried like one, because what a
# caller has to do about it is the same kind of thing as a refusal: report it,
# name the file, and let the operator decide.
CONFIG_STALE_ERROR = "config_stale"
# A configuration write that would have turned a permission on. Its own
# error_type rather than a `permission_denied`, because the grant is usually
# there: what is refused is the direction, and a caller told "permission denied"
# would go looking for the permission that opens it. There is none.
CONFIG_WIDENING_ERROR = "permission_widening_denied"
# The one command that puts a narrowed permission back, named wherever a narrowing
# is reported so that "only a human can undo this" is never left as an abstraction.
# It regenerates from attached hardware, so it is also the command that repairs a
# configuration nobody can change any more.
CONFIG_REOPEN_COMMAND = "agentic-hil init --force"
# And the surgical one beside it (hardci-hq#102). `init --force` returns the whole
# file to the generated defaults by rewriting it from hardware discovery, which on a
# bench somebody grew is not a repair but a loss: baudrate, `resource_id`,
# `state_root` and the artifact roots go with it. These two name one permission
# and move that one. Both directions ship together on purpose — a command that
# only opened would be the next one-way street, in the other direction.
CONFIG_GRANT_COMMAND = "agentic-hil grant"
CONFIG_REVOKE_COMMAND = "agentic-hil revoke"
# The refusal the two above raise while somebody on this machine holds this
# bench. Its own error_type rather than `config_write_in_open_run`, for the
# reason `config_reload_in_open_run` is its own: the rule is the same and the
# remedy is not. That one is a server refusing itself and can name
# `bench_run_stop`; this is a command line refusing because *another* process
# holds the bench, and what an operator does about that is find out whose it is.
PERMISSION_CHANGE_IN_OPEN_RUN = "permission_change_in_open_run"

# Validated flashing and unrestricted debugger access are mutually exclusive
# policies (docs/security-design.md): while either of these is true on a probe,
# `flash_firmware` on that probe is refused, and so is a debug session.
#
# They are therefore the two exceptions to the allow-by-default generation of
# hardci-hq#96, and a generated configuration writes both false. For a while it
# did not, and the two rules met for the first time on the bench: #96 opened
# every permission, this interlock reads both of these as a reason to refuse, and
# the result was that a freshly generated configuration could not flash at all
# (hardci-hq#107). The interlock was written when the pair defaulted to false, so
# "both on" meant somebody had chosen it; the inversion made it the shipped
# state without either rule changing.
#
# What settles which side gives way is that neither flag grants anything. There
# is no MCP tool for raw debugger commands and none for mass erase, so every read
# of either is a deny site: turning one on subtracts flashing and adds no
# capability in exchange. Allow-by-default is for permissions that can do
# something, and a permission whose only reachable effect is to switch another
# one off does not belong in "everything open".
#
# This tuple is the single source of truth for that. `config.py` derives the
# generated value of every debugger permission from it, so the template, the
# hardware-discovery path and `project_config_create` cannot drift apart again —
# which is exactly how they came to disagree in the first place.
EXCLUSIVE_FLASH_PERMISSIONS = ("allow_raw_debugger_commands", "allow_mass_erase")

# `can_buses.<name>.listen_only: true` is the one configuration flag whose entire
# value is that it is a proof rather than a preference, so the two ways it can
# fail to be one are named separately (hardci-hq#103). `unsupported` is settled
# before the bus is touched — the adapter has no such mode on this host, so the
# refusal is clean and `retry_safe`. `unconfirmed` is settled after: the adapter
# was asked, was already on the bus by the time it could answer, and did not
# confirm; it is closed again and the exposure named. Silently downgrading either
# to "listening anyway" is the defect these exist to prevent.
LISTEN_ONLY_UNSUPPORTED_ERROR = "can_listen_only_unsupported"
LISTEN_ONLY_UNCONFIRMED_ERROR = "can_listen_only_unconfirmed"
# A SocketCAN channel that is not a netdev on this host. Its own error_type
# rather than a shade of `can_adapter_open_failed`, because it is the one open
# failure whose outcome is not merely unproven: the bind had nothing to bind to,
# so no controller was addressed and the bench is untouched (hardci-hq#127).
CAN_INTERFACE_NOT_FOUND_ERROR = "can_interface_not_found"
# What `hardware_recover` answers when the incident needs somebody at the bench
# (hardci-hq#128). Not `permission_denied`: no grant on this or any bench opens
# it, because the missing thing is a statement about a physical board and not an
# authorization. The refusal carries the command the person runs instead.
RECOVERY_PHYSICAL_CHECK_ERROR = "recovery_requires_physical_check"


def recovery_operator_command(quarantine_id: str | None) -> str:
    """The exact line the operator types, with this incident's id in it.

    One spelling, built in one place: a refusal that named the command with a
    placeholder left the reader to find the id, and a refusal that spelled the
    flags differently from the CLI's own parser sent them to a usage error."""
    return f"agentic-hil recover --confirm-safe-state --quarantine-id {quarantine_id or '<id>'}"


def exclusive_permission_summary(action: str, blocking: str, debugger_id: str | None) -> str:
    """Why an action is refused by a permission that is *granted*, and the fix.

    One text for the four backends that raise it, because a refusal an operator
    meets on their first flash must not read differently depending on which
    programmer their board happens to use."""
    entry = f"debuggers.{debugger_id or '<name>'}.permissions.{blocking}"
    return (
        f"{action} is disabled while {blocking.removeprefix('allow_')} is allowed on this probe: validated flashing and "
        f"unrestricted debugger access are mutually exclusive policies, which is why a generated configuration leaves "
        f"both false. Something on this bench set `{entry}` to true since. Set it back to false — with "
        "`project_config_set`, or by asking the operator — and this works. Nothing here can set it back to true "
        "afterwards."
    )
# The scope that separates "this project has no configuration", which
# `project_config_create` answers, from "the configuration this running server
# loaded is gone from disk", which it must not.
CONFIG_RUNNING_SERVER_SCOPE = "running_server"
# Said by `doctor`, which parses the file at the moment it is asked and is
# therefore always current — which is exactly why it cannot speak for a server
# that has been running since before the last edit. It names both ways across,
# because since `project_config_reload_description` a restart is no longer the
# only one, and this is the last place that said it was.
RUNNING_SERVER_COMPARISON = (
    "This is the configuration as it is on disk right now; the command line reads it fresh on every invocation. A "
    "running MCP server does not: it keeps serving what it parsed at startup. Compare the `loaded_digest` here with "
    "`config_status.loaded_digest` in that server's `debugger_info`. If they differ, that server is enforcing an older "
    "configuration. Two things change that, and which one depends on what moved: `project_config_reload_description` "
    "on that server adopts a changed `target`, `debuggers`, `com_ports` or `can_buses` without a restart, and a "
    "restart adopts everything else — every `permissions:` block included, which that call never re-reads in either "
    "direction. `agentic-hil config-reload` previews what that call would take from this file and lists the sections "
    "it would leave for a restart."
)


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
        values = _substitutions()
        payload: JsonObject = {"meaning": self.meaning.format(**values), "remediation": [step.format(**values) for step in self.remediation]}
        if self.do_not:
            payload["do_not"] = [step.format(**values) for step in self.do_not]
        return payload


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def safe_user_root() -> str:
    """The per-user root this tool creates for itself on either platform.

    Named in remediation as somewhere a configuration or a ``state_root`` can be
    put when the discovered default cannot be used — a redirected profile
    directory, a roaming share, a location the operator would rather not use.
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
        "reopen_command": CONFIG_REOPEN_COMMAND,
        "grant_command": CONFIG_GRANT_COMMAND,
    }


# Keys are "<error_type>" or "<error_type>:<scope>", where scope is the config
# field the error names or the debugger backend that raised it. Lookup falls back
# from the scoped key to the bare one, so a scope nobody wrote an entry for still
# gets the general fix instead of nothing.
ERROR_CATALOGUE: dict[str, ErrorRemedy] = {
    CONFIG_STALE_ERROR: ErrorRemedy(
        meaning=(
            "The authoritative configuration on disk is not the one this MCP server is enforcing, so the backend it "
            "names, the devices it knows and the permissions it enforces are the ones from an older document. Which "
            "document is in `config_status.description_source`: `startup` means all of it came from the version parsed "
            "at startup, which is the normal case because the server does not reload while it runs; "
            "`description_reload` means the devices and the backend came from the last explicit "
            "`project_config_reload_description` (`description_reloaded_at`, and `loaded_digest` is that document's) "
            "while the permissions still came from startup (`loaded_at`). Nothing failed; what an answer says and what "
            "the file says have come apart, and the file is the one an operator reads. This says the two digests differ "
            "and nothing more — not what the file now contains, and not what a restart onto it would produce."
        ),
        remediation=(
            "If what changed is the description of the bench — `target`, a `debuggers`, `com_ports` or `can_buses` "
            "entry, a probe id, a COM device, a baudrate — call `project_config_reload_description`. It re-reads those "
            "four sections and clears this, without a restart and without touching a single permission. Its result "
            "names anything in the file it did not take.",
            "Otherwise, ask the operator to restart the MCP server, then repeat the call. Say which server: the one "
            "the agent host started for this workspace, not the `agentic-hil` command line, which reads the file fresh "
            "every time and is already current. A restart is what adopts a changed permission, a changed `version`, "
            "and every section outside those four.",
            "If the restart does not come up, the startup refusal names what is wrong with the file — that is the "
            "message to report, and repairing the file is what the restart was waiting for. `agentic-hil doctor` "
            "reads the file fresh and produces the same refusal without stopping anything.",
            "Until one of the two has happened, treat what this server says about the bench as the older file's "
            "answer. Do not reconcile the difference by guessing which of the two is right — `config_status.path` "
            "names the file and `loaded_digest` versus `current_digest` says they differ.",
            "If the change was made through `project_config_set` or `project_config_create` in this session, this is "
            "the same fact those results reported as `reload_required`, not a second problem.",
        ),
        do_not=(
            "Do not carry on flashing, resetting or driving devices on the assumption that the new file is in force. "
            "It is not, and the permissions in force are the older ones — which may be wider than what the operator "
            "has just written.",
            "Do not expect `project_config_reload_description` to take a permission. It re-reads the description and "
            "nothing else, in either direction, and there is no argument that changes that; a permission the file now "
            "states is adopted by a restart and by nothing on this surface.",
            "Do not try to make the server pick the file up by editing it again, by deleting it, or by calling a "
            "configuration write tool. Those two calls are the whole of what rebinds a running server.",
        ),
    ),
    f"config_file_not_found:{CONFIG_RUNNING_SERVER_SCOPE}": ErrorRemedy(
        meaning=(
            "The authoritative configuration this server loaded is no longer at its path. This is not a project "
            "without a configuration: this server has one, in memory, and is still enforcing it. What is gone is the "
            "file an operator would read to find out what that policy says."
        ),
        remediation=(
            "Ask the operator to put the file back at the path in `config_status.path`, or to say that its removal was "
            "intended, and then restart the MCP server. A restart before the file is back cannot succeed: there is no "
            "document to load.",
            "Report what the current policy still allows from `project_config_describe`, which answers out of the "
            "loaded policy while the file is absent: `permissions_in_force` is what is being enforced, and "
            "`document_source: loaded_policy` says it came from memory rather than from a file. `writable_keys` is "
            "empty because no configuration key can be changed while there is nothing to change.",
        ),
        do_not=(
            "Do not call `project_config_create` to replace it. A server bound to a configuration is gated by the "
            "permissions it loaded even when the file is gone, so that call is refused — and if it were not, it would "
            "replace every narrowing an operator had asked for with the generated skeleton.",
        ),
    ),
    f"config_unreadable:{CONFIG_RUNNING_SERVER_SCOPE}": ErrorRemedy(
        meaning=(
            "The authoritative configuration this server loaded is still at its path and can no longer be read, so "
            "whether it still matches what is being enforced is unknown. Unknown is not unchanged. The server keeps "
            "enforcing the version it loaded."
        ),
        remediation=(
            "Report `config_status.backend_error`: it names what the read failed on — a permission on the file or a "
            "directory above it, a path component that is no longer a directory, bytes that are no longer UTF-8.",
            "Ask the operator to make the file readable again and restart the MCP server, so that what is enforced and "
            "what can be read are the same document.",
        ),
        do_not=(
            "Do not treat an unreadable file as an unchanged one and carry on as if the answers were current.",
        ),
    ),
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
    "upgrade_blocked_by_pin": ErrorRemedy(
        meaning=(
            "The package manager holds this installation at one exact version, so the upgrade command it was given "
            "cannot move it and did not. `uv tool upgrade` reports that as success — exit code 0 with the reason on "
            "stderr — because for it, nothing to do is not a failure. The version this installation runs is unchanged; "
            "`previous_version` and `version` on this result are the same number, and that is what makes this a "
            "refusal rather than an upgrade. The pin comes from the requirement the installation was created with: "
            "`uv tool install \"agentic-hil==X.Y.Z\"` records `==X.Y.Z` and every later `uv tool upgrade` honours it."
        ),
        remediation=(
            "Close the agent host first. The command below reinstalls the environment, which on Windows means deleting "
            "it, and that delete fails while the MCP server the host started is still running out of it — the failure "
            "`installation_in_use` exists to prevent, and this command has no such check of its own.",
            "Run the command in `reinstall_command` on this result. It is the one that clears the pin *and* keeps this "
            "installation's extras: `installed_extras` says which ones were found, and reinstalling without them "
            "removes what they installed — on a bench with `can`, that silently takes CAN support away. The hint `uv` "
            "prints names the bare distribution and would do exactly that, so use ours and not that one.",
            "Then run `agentic-hil --version` to confirm the number moved, and start the agent host, which loads the "
            "new server. Nothing was restarted or reloaded by this attempt; there is nothing new to load yet.",
            "Later upgrades work normally: the reinstall records an unpinned requirement, so `agentic-hil upgrade` "
            "moves the installation from then on.",
        ),
        do_not=(
            "Do not report this as an upgrade, and do not ask the operator to restart anything on the strength of it. "
            "The installation still runs the version it ran before, with that version's behaviour and its refusals.",
            "Do not run `uv tool install agentic-hil@latest` from `uv`'s own hint. It names the distribution without "
            "extras, and `uv` records the requirement literally, so it uninstalls whatever `[can]` or `[pyocd]` "
            "brought in.",
            "Do not have Agentic HIL rewrite the installation on the operator's behalf, and do not reach for "
            "`--force` to make the pinned upgrade take. Which version a machine runs is the operator's decision; a "
            "pin can be deliberate, and an upgrade that replaces an installation nobody asked it to replace is a "
            "bigger surprise than the one being reported here.",
        ),
    ),
    "permission_denied:allow_upgrade": ErrorRemedy(
        meaning=(
            "`permissions.allow_upgrade` is false in the authoritative configuration, so this server may not replace "
            "the installation it is running out of. Nothing was attempted and the installed version is unchanged. It "
            "is a decision about who performs the maintenance of this bench's software, not about which release it "
            "should be on."
        ),
        remediation=(
            "Report the refusal, name `permissions.allow_upgrade`, and say which file carries it. The operator opens "
            "it by editing that file; you cannot, and `project_config_set` writes only `false` into a permission.",
            "The command line does the same job for a person and needs no permission at all: `agentic-hil upgrade` "
            "from any directory. It preserves the extras the installation was created with, and it refuses while this "
            "server is still running out of the installation, so it is run with the agent host closed.",
            "`agentic-hil --version` beside this result's `running_version` is how an operator checks whether a newer "
            "release is even the difference they are chasing.",
        ),
        do_not=(
            "Do not run `uv tool upgrade`, `pipx upgrade` or `pip install --upgrade` through a shell instead. This is "
            "the same action the configuration just refused, taken where the operator cannot see or audit it — and "
            "the bare forms of those commands drop the extras this bench was installed with.",
            "Do not ask for `allow_upgrade` to be turned on as a precondition for the work in hand. Whatever was "
            "being done is not blocked by the version; if it genuinely is, say which behaviour you need and let the "
            "operator decide about the upgrade separately.",
        ),
    ),
    "upgrade_in_open_run": ErrorRemedy(
        meaning=(
            "`server_upgrade` was called while something holds this bench: a declared run, an open COM or CAN "
            "session, a debug session, or another process on this machine. Those holds were taken under the release "
            "this server is running, and replacing the code underneath them would move the rules during the run they "
            "govern — the same objection as changing a permission mid-run. Nothing was replaced and the holder was "
            "not disturbed."
        ),
        remediation=(
            "Finish the run and close it with `bench_run_stop`, stop any COM or CAN session with "
            "`com_session_stop` / `can_session_stop` and any debug session with `debug_stop_session`, then repeat the "
            "upgrade.",
            "`bench_run_status` says whether a run is open on this server; `held_devices` and `device_holds` in this "
            "refusal name what is held whoever holds it, including another process.",
            "Nothing was lost by the refusal. The installation is untouched and the upgrade is exactly as available "
            "after the run as it was before it.",
        ),
        do_not=(
            "Do not end a run early only to get the upgrade through. The run is holding a board for a reason, and no "
            "release is urgent enough to abandon hardware in an unconfirmed state.",
            "Do not run the package manager through a shell to get past this. That is the same replacement without "
            "the check, under measurements that were started on the code being replaced.",
        ),
    ),
    "upgrade_cli_only_on_host": ErrorRemedy(
        meaning=(
            "This host is Windows, where the operating system refuses to delete a file that is mapped as a running "
            "image — and this server is running out of the very installation an upgrade would replace. A package "
            "manager that removes the environment before rebuilding it fails on that delete part way through and "
            "leaves neither the old installation nor the new one, so nothing was attempted. The alternative, a helper "
            "that swaps the files after this server exits, was rejected: it outlives the result that announced it, so "
            "a failure would have nobody to report to and would produce exactly the half-replaced environment this "
            "refusal exists to prevent."
        ),
        remediation=(
            "Ask the operator to run `agentic-hil upgrade` at a shell with the agent host closed. It runs as a "
            "separate process, so it can replace this installation; it upgrades through the manager that owns the "
            "installation and keeps the extras it was created with; and if a server is still running out of it, it "
            "refuses with `installation_in_use` and names the holding process rather than breaking the environment.",
            "Then the host is started again, which loads the new server. Report `running_version` from this result as "
            "the version still in force until that has happened.",
        ),
        do_not=(
            "Do not retry this tool after closing a run or a session. The refusal is about the platform, not about "
            "what the bench is doing, and it will be the same answer every time on this host.",
            "Do not reach for `uv tool install --force` or delete the environment to work around the lock. Removing "
            "an environment around a live process is the outcome being refused here, not a way past it.",
        ),
    ),
    "config_file_not_found": ErrorRemedy(
        meaning=(
            "This workspace has no authoritative configuration, so there is no bench, no permission and no state "
            "directory for the call to use. It is the first wall a new project hits, not a fault."
        ),
        remediation=(
            "Over MCP, call `project_config_create` once. It takes no arguments, generates the configuration out of "
            "the hardware attached to this machine, and writes every permission true except "
            "`allow_raw_debugger_commands` and `allow_mass_erase`, which it writes false so that flashing works, so "
            "the bench is workable from the file it produces without anyone editing YAML.",
            "On the command line a person runs `agentic-hil init` from the project root, which does the same thing.",
            "Then report what it granted — flashing and resetting among them — and ask the operator which of those "
            "this bench should not have. You can write `false` into any of them with `project_config_set`; you can "
            "never write `true`. Raw debugger commands and mass erase are already false and no tool here reads "
            "either as permission to do anything, so do not report them as missing.",
        ),
        do_not=(
            "Do not write the configuration by hand, and do not drive the hardware another way while the project has "
            "none. A missing configuration is the absence of policy, not permission to act without it.",
            "Do not delete or move an existing configuration to reach this state. What comes back is the generated "
            "skeleton, so it throws away every narrowing the operator had asked for and gives you nothing you did not "
            "already have.",
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
            "Do not set the grant through `project_config_set` either. It is a permission, and that call writes only "
            "`false` into a permission — never `true`, whatever `allow_config_permissions_write` says. A generated "
            "configuration grants that key, so it is very likely open here and still not a way back to this one.",
        ),
    ),
    "permission_denied:allow_config_permissions_write": ErrorRemedy(
        meaning=(
            "A field-wise configuration change reached a permission — what the bench may be told to do — and "
            "`permissions.allow_config_permissions_write` is false. That right gates every permission key in the file, "
            "not only the ones inside a `permissions:` block: the project block, each entry's own block under "
            "`debuggers`, `com_ports` and `can_buses`, and the two grants that sit directly on a section, "
            "`artifacts.allow_upload` and `debug.allow_all_symbols`. This is the deliberate half of the split: a "
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
    "permission_denied:allow_recover": ErrorRemedy(
        meaning=(
            "`hardware_recover` was called and `permissions.allow_recover` is false, so this bench does not let an "
            "agent clear its incidents at all. The quarantine is unchanged and every hardware effect stays blocked "
            "until an operator recovers it from the command line. This is a bench that has decided its incidents are "
            "a person's business, and the refusal is that decision working."
        ),
        remediation=(
            "Relay `operator_command` to the operator — it is the whole line, with this incident's id already in it — "
            "together with the `quarantine_guidance` from `get_last_report` or the refusal that quarantined the bench: "
            "what was attempted, what is confirmed, what is unknown, and what to check on the board.",
            "Read the incident out with `get_last_report` and `classify_last_error` while you wait. Explaining what "
            "happened is the part of the task that is still possible.",
            "If this bench should let an agent clear the no-contact class, that is an operator's change to "
            "`permissions.allow_recover` and not one you can make: `project_config_set` writes only `false` into a "
            "permission.",
        ),
        do_not=(
            "Do not delete or edit the lease records, the quarantine markers or the recovery ledger under "
            "`state_root`. That is not recovery, it is erasing the record that a bench needs checking.",
            "Do not drive the probe, the serial port or the CAN adapter outside Agentic HIL to 'get on with it'. The "
            "quarantine exists because the physical state is unknown, and the tools are not what makes it so.",
        ),
    ),
    RECOVERY_PHYSICAL_CHECK_ERROR: ErrorRemedy(
        meaning=(
            "`hardware_recover` was allowed to run and refused on the class of the incident, not on a permission. At "
            "least one reason this bench is quarantined for names a physical state — a flash whose outcome was never "
            "confirmed, a session that died mid-call, cleanup nobody could verify — and clearing it means attesting "
            "that the board is still and holds the firmware somebody expects. That is a claim about the world, and "
            "only a person at the bench can make it. `physical_check_reasons` names the ones that need it and "
            "`agent_clearable_reasons` the ones that would not have; no grant on any bench moves that line, which is "
            "why this tool has no confirmation argument to pass. A reason this bench's `recovery.auto_recover` policy "
            "could settle is refused here too and for a different reason — settling it means running a predicate "
            "against the board, and the automatic path does that on the next hardware call while this call runs "
            "nothing. `auto_recoverable` on the refusal says which of the two situations this is."
        ),
        remediation=(
            "Read `auto_recoverable` first. When it is true this bench's own recovery policy can settle the incident "
            "by running a predicate against the board — a re-read of the probe, or a verified reset into halt — and it "
            "does that on the next hardware call, not here: this tool runs no predicate, so it will not assert an "
            "unconfirmed board is fine. Retry the hardware call once and read the result.",
            "Otherwise relay `operator_command` verbatim. It is the exact command, with this incident's "
            "`quarantine_id` already in it, and it is what the operator runs after the check.",
            "Relay `quarantine_guidance` with it, per reason: what was attempted, what is still confirmed, what nobody "
            "on this host can know, and the `physical_check` to perform on the board. A signature over a claim the "
            "signer was not shown is worth nothing.",
            "Say plainly that hardware effects stay blocked until then, and stop there. This refusal is the answer to "
            "the request, not an obstacle in front of it.",
        ),
        do_not=(
            "Do not call `hardware_recover` again hoping for a different class. The reasons are read from the "
            "incident record and repeating the call cannot change them.",
            "Do not retry the hardware call to 'clear' the incident. The bench's own recovery policy already tried "
            "whatever it could verify without an operator, before this refusal existed.",
            "Do not clear the state files under `state_root` by hand, and do not ask the operator to. The command "
            "above is the supported route and it keeps the ledger line that says who signed for what.",
        ),
    ),
    CONFIG_WIDENING_ERROR: ErrorRemedy(
        meaning=(
            "A field-wise configuration change tried to write something other than a narrowing into a permission. A "
            "generated configuration starts with every permission granted but the two that refuse flashing while they "
            "are true, and the one direction `project_config_set` "
            "leaves is narrowing, so an agent writes `false` into a permission and never `true`. Two cases share this "
            "error because for a caller they are one fact — nothing here turns a permission on. Either the request "
            "carried a permission value that was not `false`, and `widened_keys` names those keys as the request spelled "
            "them, including a `true` sent to a permission that is already `true` and would have moved nothing; or the "
            "resulting document would have granted more than the current one, and `widened_keys` names the paths that "
            "would have opened, read out of the document before and after rather than out of the request. Nothing was "
            "written in either case, including the parts of the same call that were narrowings — a change is applied "
            "whole or not at all."
        ),
        remediation=(
            "If the intent was to narrow the bench, re-send the call with `false` values only; a call that mixes the "
            "two is refused for the widening and loses the narrowing with it.",
            "If a permission really has to come back, that is a person's decision at the command line: "
            "`{grant_command} <key>` opens that one permission in the file as it stands, and `{reopen_command}` "
            "regenerates the configuration from attached hardware at the generated defaults again. Report which "
            "permission is needed and why, name the key, and stop.",
            "`project_config_describe` says what this configuration grants right now, so the report names the actual "
            "gap rather than a guess at one.",
        ),
        do_not=(
            "Do not close a permission and reopen it later to work around something. You cannot: this refusal covers a "
            "permission you narrowed yourself a moment ago exactly as it covers one you never touched.",
            "Do not look for another key, another spelling or another tool that reaches the same value. The comparison "
            "is made on the permissions present in the document, not on the keys the request named, so every route "
            "lands here.",
            "Do not carry out the action the permission would have allowed by another route. A debugger, serial device "
            "or CAN adapter driven outside Agentic HIL defeats the policy this refusal enforces.",
        ),
    ),
    "debugger_config_not_found": ErrorRemedy(
        meaning=(
            "A debugger script this entry names could not be used: the interface or target configuration file the "
            "backend was pointed at is not where the configuration says it is. Nothing was run and the bench was not "
            "touched."
        ),
        remediation=(
            "Check `debuggers.<name>.interface_cfg` and `.target_cfg` against what is installed on this machine; "
            "`agentic-hil doctor` names the entry and the file it could not use.",
            "Set them with `project_config_set` to the absolute paths of the scripts for this probe and target. A "
            "configured script path must be absolute and must live outside the workspace.",
        ),
        do_not=(
            "Do not copy OpenOCD scripts into the repository and point the configuration at them. A script inside the "
            "workspace is repository-controlled Tcl running in the debugger, and a configured absolute path inside the "
            "workspace is refused at load for that reason.",
            "Do not run `openocd` directly to get past it.",
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
    PERMISSION_CHANGE_IN_OPEN_RUN: ErrorRemedy(
        meaning=(
            "`{grant_command}` or `agentic-hil revoke` was run while something on this machine holds this bench: an MCP "
            "server with a declared run, an open COM or CAN session, a debug session, or another terminal. Those holds "
            "were taken under the permissions this file states, so opening or closing one underneath them would move "
            "the rules during the run they govern. Nothing was written and the holder was not disturbed. The same rule "
            "as `config_write_in_open_run`; a separate error type because the holder is another process, so the remedy "
            "is to find out whose it is rather than to close a run this caller has."
        ),
        remediation=(
            "`agentic-hil lease-status` names the holder — which devices are held, which frontend took them and under "
            "which process — and `open_holds` in this refusal carries the same.",
            "Let the run finish, or ask whoever holds it to close it: `bench_run_stop` for a declared run, "
            "`com_session_stop` / `can_session_stop` for a session, `debug_stop_session` for a debug session. Then "
            "repeat the command.",
            "Nothing was lost by the refusal. The file is unchanged and the permission is exactly as changeable after "
            "the run as it was before it.",
        ),
        do_not=(
            "Do not stop somebody else's run only to get the permission change through. A board mid-flash left in an "
            "unconfirmed state costs more than waiting, and no permission change is urgent enough to buy it.",
            "Do not edit the configuration by hand instead. That is the same write without the lock check, the "
            "provenance record or the validation, and it is what these commands exist to replace.",
        ),
    ),
    "config_reload_in_open_run": ErrorRemedy(
        meaning=(
            "`project_config_reload_description` was called while this server holds hardware: a declared run, an open "
            "COM or CAN session, or a debug session. Those holds name devices by their configuration entry, and the "
            "description on disk decides which physical unit each of those names means — so re-reading it mid-run could "
            "point a held name at another board. Nothing was re-read, nothing was written, and the run is untouched. "
            "The same rule as `config_write_in_open_run` and the same remedy; it is a separate error type only because "
            "this call writes nothing, and a caller should not be told it did."
        ),
        remediation=(
            "Finish the run and close it with `bench_run_stop`, stop any COM or CAN session with "
            "`com_session_stop` / `can_session_stop` and any debug session with `debug_stop_session`, then repeat the "
            "reload.",
            "`bench_run_status` says whether a run is open and which devices it declared; the refusal carries the "
            "same in `open_holds`.",
            "Nothing was lost by the refusal. The file is unchanged and the reload is exactly as available after the "
            "run as it was before it.",
        ),
        do_not=(
            "Do not end a run early only to get the reload through. The run is holding a board for a reason, and a "
            "description that is one call newer is never urgent enough to abandon hardware in an unconfirmed state.",
            "Do not ask for a server restart instead. A restart under an open run is strictly worse than this refusal: "
            "it drops the run's holds without closing them.",
        ),
    ),
    "unsafe_configured_path": ErrorRemedy(
        meaning=(
            "A configured path is not the kind of object it has to be: a component of it is a symlink, or is a file "
            "where a directory was needed, or the final object is not a single-link regular file. The path was refused "
            "before anything was read from it or written to it, because following it would act on an object other than "
            "the one the configuration names. "
            "This is about what the path *is*, not about who else on the machine holds rights on it. That second "
            "question used to be asked — a Windows ACL walk and a POSIX mode/sticky-bit walk over every ancestor — and "
            "it was removed in 0.8.0 (hardci-hq#95): it could only ever defend against a different account on the same "
            "machine, which was never a requirement here, and it could never defend against the operator's own "
            "processes, which own these objects and can rewrite them regardless of any ACL."
        ),
        remediation=(
            "Read `path`, and `component` when the refusal carries one: that is the part of the chain that stopped the "
            "walk. Replace the symlink with a real directory, or point the setting at a path that does not go through "
            "it.",
            "{safe_user_root} is a location this tool creates for itself and is a safe answer when the discovered "
            "default cannot be used.",
            "Re-run the command that failed. Nothing else has to change.",
            "Where each file may live: MCP resource " + PLATFORM_PATHS_URI + ".",
        ),
        do_not=(
            "Do not delete or overwrite whatever stands at the named component. It is something the operator or "
            "another program put there, and the refusal is a report about the path, not a request to clear it.",
            "Do not move the authoritative configuration or state_root inside workspace_root to get past this. Both "
            "are refused there for a reason that has nothing to do with this failure: repository content would then "
            "be able to rewrite the policy that governs the hardware.",
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
    "com_port_identity_mismatch": ErrorRemedy(
        meaning=(
            "The device name in a COM port entry currently leads to different hardware than the entry says it belongs "
            "to. `expected_serial_number` is what the configuration names and where it says so (`expected_from`); "
            "`found_serial_number` is the adapter actually behind `configured_device` right now. The port was not "
            "opened and nothing was written to it. A device name — `COM7`, `/dev/ttyACM0` — is an enumeration order "
            "rather than an identity, so attaching a second adapter or replugging in another order can hand one entry "
            "another board; this refusal is that having happened."
        ),
        remediation=(
            "Read `expected_device` when it is present: the board this entry names is still attached, under that name, "
            "and the entry is simply out of date. `agentic-hil adopt-hardware` rewrites it from the attached hardware.",
            "Without `expected_device` the named board is not attached at all. Plug it in, or work on the board that is "
            "there by naming its own entry.",
            "On Linux, prefer `/dev/serial/by-id/usb-<vendor>_<product>_<serial>-ifNN` for `device`. udev builds that "
            "name from the device's own serial, so it follows the board instead of the enumeration order.",
        ),
        do_not=(
            "Do not change `serial_number` to the serial that was found, and do not delete the key. That turns the one "
            "check that noticed into agreement with whatever is plugged in, which is the silent wrong-board flash this "
            "refusal exists to prevent. Change the board or change `device`; the identity is not the thing to edit.",
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
    # The counterpart to the three entries above, and the reason it is a separate
    # error_type rather than another `target_not_detected`: those say the adapter
    # was reached and nothing answered behind it, which places the abort point
    # before the target and is why they refuse retry-safe. A toolchain that exited
    # without confirming what it did places it nowhere — whether it printed part of
    # the confirmation or none of it — so the same public error_type would have
    # published exactly the claim this branch cannot make.
    "target_state_unconfirmed:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD exited successfully without the tool's own success marker, so its account of this run is "
            "incomplete rather than negative. Which part is missing is in this result and not in this entry: "
            "`operation_result.expected_success_text` lists both markers the backend `echo`es — the stage marker after "
            "`init` and the success marker at the end of the command — and `matched_success_text` names the ones "
            "OpenOCD printed, which may be neither of them or the stage marker alone. Either way the outcome went "
            "unreported, and that is the absence of a verdict rather than a verdict that no target answered: `init` "
            "may have completed, examined the core and halted it. The board's run state is unknown, which is why this "
            "quarantines the bench instead of refusing."
        ),
        remediation=(
            "Read `quarantine_guidance` in this result first: it names what is confirmed, what is not, and the physical "
            "check to make on the board before anyone signs `agentic-hil recover --confirm-safe-state`.",
            "Read `operation_result` in this result: the markers that did print bound how far the run provably got, and "
            "the stage marker among them means `init` completed and the core was examined.",
            "Read the debugger log at `log_path`. Both markers are `echo`ed by the command string this backend sends, "
            "so whatever OpenOCD printed instead of the missing one is the evidence for how far it actually got.",
            "Confirm `debuggers.<name>.executable` is OpenOCD itself and not a wrapper or launcher script: anything "
            "that discards the child's stdout and stderr produces this result out of a run that worked.",
            "If the log does contain the success marker `matched_success_text` reports as missing, this is a defect in "
            "this backend rather than a fault on the bench — report it with that log.",
        ),
        do_not=(
            "Do not read this as `target_not_detected`. That is OpenOCD's own report that it reached the adapter and "
            "nothing answered, which places the abort point before the target and is why it is retry-safe; this result "
            "places it nowhere.",
            "Do not try to clear the quarantine with another read. A probe that answers attests the board is "
            "reachable, never that a core this run may have halted is running again.",
        ),
    ),
    "target_state_unconfirmed:stlink": ErrorRemedy(
        meaning=(
            "STM32CubeProgrammer exited successfully without every line that confirms the operation — for a read, the "
            "ST-Link serial number and the device name — so its account of this run is incomplete rather than "
            "negative. Which lines are missing is in this result and not in this entry: "
            "`operation_result.expected_success_text` lists the ones that were looked for and `matched_success_text` "
            "the ones the CLI printed, which may be none of them or only some. A confirmation that stops short is not "
            "a report that no target answered: it leaves the outcome unstated, so this is the absence of a verdict and "
            "the board's run state is unknown. That is why this quarantines the bench instead of refusing."
        ),
        remediation=(
            "Read `quarantine_guidance` in this result first: it names what is confirmed, what is not, and the physical "
            "check to make on the board before anyone signs `agentic-hil recover --confirm-safe-state`.",
            "Read `operation_result` in this result and then the debugger log at `log_path`: `expected_success_text` "
            "lists the lines that were looked for, `matched_success_text` the ones that arrived, and what the CLI "
            "printed in place of the rest is the evidence for how far it got.",
            "Confirm `debuggers.<name>.executable` is STM32_Programmer_CLI itself and not a wrapper that discards its "
            "output. A CLI version that words its confirmation differently produces the same result, and the log is "
            "what tells the two apart.",
        ),
        do_not=(
            "Do not read this as `target_not_detected`. `No STM32 target found` is the CLI's own report that the probe "
            "was opened and nothing answered behind it, which is why that one refuses retry-safe; silence is not that "
            "report.",
            "Do not try to clear the quarantine with another read. A probe that answers attests the board is "
            "reachable, never that a core this run may have halted is running again.",
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
    # -- The one flag whose whole value is that it is never silently degraded ---
    CAN_INTERFACE_NOT_FOUND_ERROR: ErrorRemedy(
        meaning=(
            "The SocketCAN interface named by `can_buses.<name>.channel` does not exist on this host. The session was "
            "refused where the socket would have been bound, so no CAN controller was addressed and nothing was put on "
            "any bus — this is a refusal about the host's network configuration, not a quarantine. The usual cause is "
            "that the interface was never brought up, or that a USB adapter was re-enumerated and its `canN` name "
            "moved."
        ),
        remediation=(
            "Read `channel` on the result: that is the interface name that was looked for.",
            "List what the host actually has with `ip link show type can`. An adapter that is plugged in but unnamed "
            "there needs its driver loaded; one under a different `canN` number needs that number in the "
            "configuration, which `project_config_set` writes into `can_buses.<name>.channel`.",
            "Bring a real interface up with `sudo ip link set <dev> type can bitrate <bitrate>`, then `sudo ip link "
            "set <dev> up`. For a bench without hardware, `sudo modprobe vcan`, `sudo ip link add dev vcan0 type "
            "vcan`, then `sudo ip link set vcan0 up`.",
            "Retry the session afterwards. The bench was never blocked: `retry_safe` is true and no incident was "
            "opened.",
        ),
        do_not=(
            "Do not run `recover --confirm-safe-state` over this. There is nothing to recover — no lease was "
            "quarantined, and signing for a physical state nobody disturbed teaches the signature to mean nothing.",
            "Do not point the entry at whichever `canN` happens to be up. That is the wrong-bus mistake the channel "
            "name exists to prevent; confirm which interface belongs to this bench first.",
        ),
    ),
    LISTEN_ONLY_UNSUPPORTED_ERROR: ErrorRemedy(
        meaning=(
            "`can_buses.<name>.listen_only: true` is configured and this adapter cannot be held to it, so the session "
            "was refused before the bus was touched. The flag is not a preference: it is the claim that observing this "
            "bus sends nothing, and a controller outside listen-only sends dominant ACK bits that decide whether a "
            "sender considers its frame delivered. A downgrade to listening anyway is the defect this refusal exists "
            "to prevent."
        ),
        remediation=(
            "Read `link_state` on the result. It says what was found, not only that something was wrong.",
            "If the bus really must not be disturbed, put the interface into listen-only outside Agentic HIL and start "
            "the session again. On SocketCAN that is `sudo ip link set <dev> down`, then `sudo ip link set <dev> type "
            "can bitrate <bitrate> listen-only on`, then `sudo ip link set <dev> up`.",
            "If the bus may be ACKed — a bench nobody else is driving, a rig where this is the only participant — set "
            "`listen_only: false` on that entry. That is an honest configuration, and the refusal goes away because "
            "nothing is being claimed any more.",
            "`can_buses_list` reports `listen_only` and `listen_only_enforcement` for every configured bus, so which "
            "adapter can back the claim, and on what evidence, is readable before a session is started.",
        ),
        do_not=(
            "Do not treat this as a reason to drop the flag and carry on reading. The reading would be the one the "
            "flag was there to prevent, and nothing downstream would say the bus had been ACKed.",
            "Do not reach past Agentic HIL to `candump` or a python-can script to get the read anyway. The controller "
            "mode is the same for them; what changes is only that no report says so.",
        ),
    ),
    f"{LISTEN_ONLY_UNSUPPORTED_ERROR}:socketcan": ErrorRemedy(
        meaning=(
            "SocketCAN's listen-only is a control mode on the kernel's CAN device, set when the interface is "
            "configured and owned by the netdev — not by the socket this process opens. python-can's SocketCAN backend "
            "accepts a `listen_only` keyword and discards it, so there is nothing here that could apply the mode. It "
            "is read instead, and this interface is not in it — or its control mode could not be read at all, which is "
            "the same refusal, because the flag exists to be a proof."
        ),
        remediation=(
            "Read `link_state` on the result: it separates `up without listen-only` from `could not be read`, and "
            "those have different fixes.",
            "To put a real CAN interface into listen-only: `sudo ip link set <dev> down`, then `sudo ip link set <dev> "
            "type can bitrate <bitrate> listen-only on`, then `sudo ip link set <dev> up`. Confirm with `ip -details "
            "link show dev <dev>`, which prints `<LISTEN-ONLY>`.",
            "If `link_state` says the control mode could not be read, install iproute2. Agentic HIL reads `ip` from "
            "/sbin, /usr/sbin, /bin or /usr/bin and deliberately not from PATH, because a proof read from a "
            "PATH-resolved binary is not a proof.",
            "A `vcan` interface has no CAN controller and therefore no listen-only mode; there is also no physical bus "
            "to disturb. Set `listen_only: false` there, and let the flag mean something only where a real controller "
            "can back it.",
        ),
        do_not=(
            "Do not bring the interface up listen-only from inside a test run. The mode holds for everything on that "
            "netdev, and an interface reconfigured mid-run is itself a change to the bus — which is what was being "
            "avoided.",
        ),
    ),
    LISTEN_ONLY_UNCONFIRMED_ERROR: ErrorRemedy(
        meaning=(
            "`listen_only: true` was requested, the adapter was asked for it, and it did not confirm the mode. The "
            "adapter was closed again, so the exposure is the open rather than a whole session — but it was on the bus "
            "for that long, and this result says so instead of implying otherwise. An unconfirmed listen-only is "
            "treated as no listen-only, because the value of the flag is the proof and not the request."
        ),
        remediation=(
            "Read `driver_state` on the result: it names what came back, which is what has to change.",
            "Check that the adapter has a listen-only mode at all. Not every CAN interface does; one that does not "
            "cannot be made to, and such a bus has to be observed with different hardware.",
            "`can_buses_list` reports `listen_only_enforcement` per adapter — what evidence would back the claim "
            "there.",
            "If the bus may be ACKed, set `listen_only: false` on that entry rather than leaving a claim nothing "
            "supports.",
        ),
        do_not=(
            "Do not retry in the hope of a different answer, and do not read the bus with `listen_only` removed while "
            "still calling the reading passive. The frames would arrive either way; what would be lost is that the "
            "reading ACKed them.",
        ),
    ),
    f"{LISTEN_ONLY_UNCONFIRMED_ERROR}:peak": ErrorRemedy(
        meaning=(
            "PCAN expresses listen-only as `PCAN_LISTEN_ONLY`, which python-can sets through `BusState.PASSIVE` and "
            "not through any `listen_only` argument. Agentic HIL sets it, re-asserts it once the channel is "
            "initialized — python-can applies it before `PCANBasic.Initialize` and discards the `SetValue` return "
            "code, so the constructor's request alone proves nothing — and then reads the parameter back. This channel "
            "did not read back as listen-only, so it was closed."
        ),
        remediation=(
            "Read `driver_state`. `reports PCAN_LISTEN_ONLY off` is the driver answering; anything about the parameter "
            "not being readable means the measurement failed rather than the mode.",
            "Confirm the PCAN hardware supports listen-only. PCAN-USB and PCAN-USB FD do; some OEM and older channels "
            "report the parameter as unsupported, and those cannot observe a bus without ACKing.",
            "Check that the PCANBasic driver and python-can are current — `python -m pip install --upgrade "
            "\"agentic-hil[can]\"` — since both the mode and the read-back go through PCANBasic.",
            "If the bench may be ACKed, set `listen_only: false` on that entry. Nothing else about the session "
            "changes.",
        ),
        do_not=(
            "Do not work around this by dropping `listen_only` and reading anyway on a bus carrying somebody else's "
            "traffic — a vehicle, a rig, hardware that is not yours. That is the exact case the flag is for.",
        ),
    ),
    f"{LISTEN_ONLY_UNCONFIRMED_ERROR}:process": ErrorRemedy(
        meaning=(
            "The CAN process bridge was sent `listen_only: true` in its `open` request and its response did not "
            "confirm the mode. A bridge is code this project did not write, so being told is not evidence: the "
            "confirmation has to come back, or the claim is unbacked and the session is refused."
        ),
        remediation=(
            "Make the bridge answer `\"listen_only\": true` in its `open` result once it has actually put its "
            "controller into listen-only. That field is what turns a forwarded request into a confirmation. The bridge "
            "protocol version does not change, and a bridge that is never asked for the mode is unaffected.",
            "If the bridge cannot provide listen-only on its hardware, it should return an error from `open` rather "
            "than a success without the field — the refusal then names the bridge's own reason instead of this one.",
            "If the bus may be ACKed, set `listen_only: false` on that entry; the request stops carrying the flag and "
            "the bridge is not asked to confirm anything.",
        ),
        do_not=(
            "Do not answer `listen_only: true` unconditionally to clear the refusal. That reintroduces the defect one "
            "process further out, where nothing in this repository can see it.",
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
    # `meaning` is substituted like the steps are. What a refusal *means* can turn
    # on the machine as much as what to do about it does — whether the discovered
    # default under %APPDATA% is itself the case being described depends on this
    # host's join state — and a placeholder that reached a reader verbatim would be
    # worse than the flat claim it replaced.
    entry["meaning"] = remedy.meaning.format(**values)
    entry["remediation"] = [step.format(**values) for step in remedy.remediation]
    if remedy.do_not:
        entry["do_not"] = [step.format(**values) for step in remedy.do_not]
    return entry


# ---------------------------------------------------------------------------
# What a quarantine reason asks the operator to verify.
#
# `agentic-hil recover --confirm-safe-state` is a signature: the operator attests
# that the physical bench is in a safe state. A signature over a claim the signer
# cannot judge is worthless, so every reason that can hold a bench carries the
# four facts the signer needs: what was being attempted when confirmation was
# lost, what is still confirmed, what nobody on this host can know any more, and
# what to check on the physical board before signing. These travel with every
# quarantined tool result and with `hardware_lease_status`; this catalogue is the
# one place the texts live.


@dataclass(frozen=True)
class QuarantineReasonGuide:
    """The four facts an operator needs before signing off one quarantine reason.

    ``attempted`` is the action whose outcome was lost, ``confirmed`` what is
    still known to hold, ``unknown`` the exact gap that makes a machine answer
    impossible — the justification for needing a human at all — and
    ``physical_check`` what that human verifies on the bench before running
    `agentic-hil recover --confirm-safe-state`.
    """

    attempted: str
    confirmed: str
    unknown: str
    physical_check: str

    def as_json(self, reason: str) -> JsonObject:
        return {
            "reason": reason,
            "attempted": self.attempted,
            "confirmed": self.confirmed,
            "unknown": self.unknown,
            "physical_check": self.physical_check,
        }


_AUDIT_CONFIRMED = (
    "Everything up to the last committed audit record happened as that record says; the failure is in persisting "
    "evidence, not a report of damage."
)
_AUDIT_UNKNOWN = (
    "Whether the actions since the last committed record reached the device, and in what order — the evidence channel "
    "itself is what broke, so no later record can answer this."
)
_AUDIT_PHYSICAL_CHECK = (
    "Fix the audit destination first (free disk space, permissions on the reports and logs directories under "
    "state_root), read the last committed report with `agentic-hil` `get_last_report`, confirm the board matches what "
    "it describes — firmware, running/halted, wiring — and only then sign."
)


def _audit_guide(attempted: str) -> QuarantineReasonGuide:
    return QuarantineReasonGuide(attempted=attempted, confirmed=_AUDIT_CONFIRMED, unknown=_AUDIT_UNKNOWN, physical_check=_AUDIT_PHYSICAL_CHECK)


_DEBUG_SESSION_PHYSICAL_CHECK = (
    "Check that no leftover debug server (OpenOCD/GDB) process is holding the probe, power-cycle or reset the target "
    "into a defined state by its own controls, confirm the expected firmware banner or LED pattern, then sign."
)
_EXCEPTION_UNKNOWN = (
    "Where the operation stopped: what had already been sent to the device and what had not. An exception carries no "
    "abort point, so the device may hold a partial effect."
)


def _discovery_exception_guide(tool: str) -> QuarantineReasonGuide:
    return QuarantineReasonGuide(
        attempted=f"`{tool}` was reading the attached probe (enumeration plus a HOTPLUG connect) when it was interrupted by an exception.",
        confirmed="The read intended no stimulus: discovery connects without resetting and writes nothing to the target.",
        unknown="Whether a toolchain process is still running and holding the probe, and whether the connect completed or aborted mid-handshake.",
        physical_check="Check for leftover debugger processes holding the probe, unplug/replug the probe if its LED shows a stuck connection, confirm the target still runs its firmware, then sign.",
    )


def _discovery_audit_guide(tool: str) -> QuarantineReasonGuide:
    return _audit_guide(f"`{tool}` read the attached probe and then could not write the audit record of that read.")


def _terminal_audit_guide(tool: str) -> QuarantineReasonGuide:
    return QuarantineReasonGuide(
        attempted=f"`{tool}` read the attached probe, released its leases cleanly, and then could not commit the record saying so.",
        confirmed="The probe read completed and every lock was handed back; the hardware itself finished in the state the read left it.",
        unknown="Nothing about the board — what is missing is the durable record; until it exists, later readers cannot distinguish this from a read that ended badly.",
        physical_check="Restore the audit destination (disk space, permissions under state_root); no board inspection is required beyond confirming the probe is idle, then sign.",
    )


QUARANTINE_REASON_GUIDES: dict[str, QuarantineReasonGuide] = {
    # -- Coordination lifecycle -------------------------------------------------
    "owner_process_exited_without_release": QuarantineReasonGuide(
        attempted="A previous Agentic HIL process held this project's hardware and exited without confirming its cleanup.",
        confirmed="The dead owner can no longer touch the device; its machine-wide device lock died with it.",
        unknown="What its last action was and whether it completed: the process ended without recording a confirmed safe state, so the board may hold a partial flash, an open session's halt, or stale stimulus.",
        physical_check="Confirm no orphaned debugger/serial/CAN process is running, power-cycle or reset the target by its own controls, verify the expected firmware runs, then sign.",
    ),
    "owner_closed_with_active_lease": QuarantineReasonGuide(
        attempted="This Agentic HIL service shut down while a hardware lease was still active.",
        confirmed="The shutdown itself released the machine-wide device lock; no Agentic HIL process is driving the device any more.",
        unknown="The state the interrupted call left the device in: the lease never reported a confirmed safe state.",
        physical_check="Verify the device is idle (no unexpected output on its console, expected firmware running), reset it by its own controls if in doubt, then sign.",
    ),
    "safe_state_unconfirmed": QuarantineReasonGuide(
        attempted="A hardware lease was released without its holder confirming the device reached a safe state.",
        confirmed="The release itself was recorded; the device is no longer being driven.",
        unknown="Whether the device is in the state the last operation intended — the holder explicitly declined to confirm it.",
        physical_check="Inspect the board for the state the last report describes (firmware, run/halt, outputs), drive it to a known state by its own controls, then sign.",
    ),
    "process_reap_unconfirmed": QuarantineReasonGuide(
        attempted="A hardware lease was released, but the toolchain processes it had started could not all be confirmed terminated.",
        confirmed="The lease's own records were committed; the device lock is back.",
        unknown="Whether a leftover child process (debug server, bridge) is still attached to the device and able to act on it.",
        physical_check="List running processes for leftover debugger/bridge children and end them, confirm the probe and port are free, then sign.",
    ),
    "audit_broken": _audit_guide("A hardware lease was released while its audit trail was broken."),
    # Literal keys, not imports from agentic_hil.coordination: this catalogue is
    # a leaf module, and the reason strings are the stable contract persisted in
    # incident records.
    "lease_release_unconfirmed": QuarantineReasonGuide(
        attempted="A clean release could not persist its own record or hand back a lock; the hardware action itself had already completed.",
        confirmed="The device saw nothing after the completed action; this is a host-side persistence fault.",
        unknown="Whether the on-disk coordination state matches memory; retrying the release settles it without touching the board.",
        physical_check="Usually none: this reason is machine-recoverable, and the next hardware call retries the release itself. If it persists, fix the state_root filesystem, then sign.",
    ),
    # -- Machine recovery and dispatch guards ----------------------------------
    "machine_recovery_failed": QuarantineReasonGuide(
        attempted="The service was verifying a recoverable incident (process reap plus probe re-read, possibly a reset into halt) and the verification itself raised.",
        confirmed="The original incident is unchanged; recovery never attested a safe state.",
        unknown="Whether the recovery's own reset or probe read reached the target before failing.",
        physical_check="Treat the board as holding the original incident: reset it by its own controls, confirm the expected firmware state, then sign.",
    ),
    "machine_recovery_audit_broken": _audit_guide("Machine recovery verified a safe state but could not persist the attestation record, so the quarantine stands."),
    "unknown_hardware_exception": QuarantineReasonGuide(
        attempted="A hardware tool call raised an exception the service could not classify.",
        confirmed="The exception was contained and reported; no further calls have touched the device since.",
        unknown=_EXCEPTION_UNKNOWN,
        physical_check="Read the failure report for the tool that raised, put the device into a known state by its own controls, confirm it responds normally, then sign.",
    ),
    "hardware_exception_audit_broken": _audit_guide("A hardware tool call failed and the failure report itself could not be persisted."),
    "lease_release_report_audit_broken": _audit_guide("A one-shot debugger call released its lease and the final report of that release could not be persisted."),
    # -- Debugger one-shots and sessions ---------------------------------------
    "debugger_readonly_result_unconfirmed": QuarantineReasonGuide(
        attempted="A read-only probe call (probe discovery or probe_target) named an abort point before the target and still returned a result the host could not settle — an unknown or partial side effect, or cleanup left outstanding.",
        confirmed="The backend's own report says the target was never contacted (`target_contacted: false`), so the board keeps the state the last effectful call left.",
        unknown="Why a call that never reached the target reported an effect at all; a read-only re-read settles it, which is why this reason is machine-recoverable.",
        physical_check="Normally none — the next hardware call re-reads the probe and clears this itself. If the probe stays unreachable, reseat it, then sign.",
    ),
    "debugger_readonly_target_state_unconfirmed": QuarantineReasonGuide(
        attempted="A read-only probe call (probe discovery or probe_target) failed without naming where it stopped: the backend was killed at its deadline, or it reported a failure that does not place the abort point before the target.",
        confirmed="The toolchain child process was reaped, so nothing from this call can still act on the board.",
        unknown="Whether the read reached the target before it stopped. A read on this bench is not passive — an SWD attach halts the core — and a process killed at its deadline never ran the shutdown in its own command string, so the core may be sitting halted with nothing to resume it.",
        physical_check="Establish the run state rather than the reachability: reset the board by its own controls and confirm the firmware runs, then sign. Under `recovery.auto_recover: reset_halt` the service settles this itself with a verified reset into halt; a bare re-read cannot, and does not clear it.",
    ),
    "debugger_result_unconfirmed": QuarantineReasonGuide(
        attempted="flash_firmware or reset_target reported an outcome the host could not confirm.",
        confirmed="The command was issued through the configured toolchain and its output was captured in the log the report names.",
        unknown="Whether the flash or reset reached the target and completed: the firmware on the board and its run state may be either the old or the new one.",
        physical_check="Check which firmware the board runs (version banner, behavior), reflash or reset by its own controls if needed, then sign. Under `recovery.auto_recover: reset_halt` the service settles this itself with a verified reset into halt.",
    ),
    "debugger_call_exception": QuarantineReasonGuide(
        attempted="A debugger call raised an exception instead of returning a result.",
        confirmed="The lease was captured before anything ran; no later call has driven the probe.",
        unknown="Whether the toolchain child process was reaped and what it sent before the exception — a returned result would have proven the child was terminated, an exception proves nothing.",
        physical_check="Check for leftover debugger processes holding the probe, confirm the target's firmware state, then sign.",
    ),
    "debug_session_start_unconfirmed": QuarantineReasonGuide(
        attempted="debug_start_session started a debug server against the target and could not confirm the session's state.",
        confirmed="What the phase fields of the failure report say: `load_phase` records how far startup provably got.",
        unknown="Whether the server halted, reset, or partially loaded firmware onto the target before failing.",
        physical_check=_DEBUG_SESSION_PHYSICAL_CHECK,
    ),
    "debug_session_result_unconfirmed": QuarantineReasonGuide(
        attempted="A debug session command (symbol read, dump) reported an outcome the host could not confirm.",
        confirmed="The session was attached under a lease and every prior command is in the session log.",
        unknown="Whether the failed command changed target state before failing.",
        physical_check=_DEBUG_SESSION_PHYSICAL_CHECK,
    ),
    "debug_breakpoint_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="Setting or clearing breakpoints could not be confirmed against the backend.",
        confirmed="The session log records every breakpoint command issued.",
        unknown="Whether hardware breakpoints remain armed on the target — firmware run under a leftover breakpoint stops where nobody expects.",
        physical_check="A successful debug_clear_breakpoints reconciled against the backend clears this without an operator; otherwise power-cycle the target so the debug unit forgets its breakpoints, then sign.",
    ),
    "debug_target_state_unconfirmed": QuarantineReasonGuide(
        attempted="debug_continue or debug_halt lost confirmation of whether the target is running or halted.",
        confirmed="The session is still owned; the command sequence up to the failure is in the session log.",
        unknown="Whether the target is currently running or halted.",
        physical_check="A successful debug_halt clears this without an operator; otherwise observe the board (heartbeat LED, console output) to see whether firmware runs, reset it by its own controls, then sign.",
    ),
    "debug_session_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="debug_stop_session could not confirm the debug server and GDB were torn down.",
        confirmed="The stop was requested and recorded; no new session can start over the remains.",
        unknown="Whether a server process still holds the probe and whether the target was left halted.",
        physical_check=_DEBUG_SESSION_PHYSICAL_CHECK,
    ),
    "debug_audit_broken": _audit_guide("A debug session status read surfaced a broken audit latch: session evidence can no longer be persisted."),
    "debug_coordination_report_audit_broken": _audit_guide("A debugger call completed but its lease-status report could not be persisted."),
    "debug_backend_cleanup_exception": QuarantineReasonGuide(
        attempted="Service shutdown raised while closing the debug backend.",
        confirmed="The shutdown error and any cleanup errors were reported to the caller that closed the service.",
        unknown="Whether the debug server was torn down and the target released.",
        physical_check=_DEBUG_SESSION_PHYSICAL_CHECK,
    ),
    "debug_shutdown_reporting_failed": QuarantineReasonGuide(
        attempted="Service shutdown stopped the debug session but could not report or release it cleanly.",
        confirmed="The backend close itself succeeded; the failure is in the closing report or release.",
        unknown="Whether the recorded state matches the session's real end state.",
        physical_check=_DEBUG_SESSION_PHYSICAL_CHECK,
    ),
    # -- COM ports --------------------------------------------------------------
    "com_open_interrupted": QuarantineReasonGuide(
        attempted="com_session_start was interrupted (for example by Ctrl-C) while opening the serial port.",
        confirmed="No stimulus was written: the session never reached a writable state.",
        unknown="Whether the OS handle was left open and whether the modem lines (DTR/RTS) were left asserted — on a board that wires DTR to reset, that holds the target in reset.",
        physical_check="Confirm no process holds the port, that the target is not held in reset (its firmware runs), then sign.",
    ),
    "com_open_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="com_session_start failed and could not confirm the partially opened port was closed again.",
        confirmed="No stimulus was written; the failure happened during open or its rollback.",
        unknown="Whether the port handle is still open and holding modem lines.",
        physical_check="Confirm the port is free (no process holds it) and the target is not held in reset, then sign.",
    ),
    "com_write_effect_unconfirmed": QuarantineReasonGuide(
        attempted="com_write raised while writing stimulus to the port.",
        confirmed="Everything before this write is in the COM log; reads are unaffected.",
        unknown="How many of the requested bytes reached the wire — the target may have received a truncated command.",
        physical_check="Check the device console/behavior for a partially applied command, bring the device to a known state by its own controls, then sign.",
    ),
    "com_buffer_clear_unconfirmed": QuarantineReasonGuide(
        attempted="Clearing the port's receive buffer failed after the OS-level clear had started.",
        confirmed="No stimulus was written; only received bytes were being discarded.",
        unknown="Whether the OS buffer was fully cleared, so a later read may mix old and new bytes.",
        physical_check="Usually none: this reason is machine-recoverable by a re-read. If it persists, close whatever else holds the port, then sign.",
    ),
    "com_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="Stopping a COM session could not confirm the port was closed and the reader stopped.",
        confirmed="The session is out of service; no further writes are possible through it.",
        unknown="Whether the OS handle and reader thread are really gone, and with them the port's modem-line state.",
        physical_check="Confirm the port is free and the target is not held in reset, then sign.",
    ),
    "com_effect_unconfirmed": QuarantineReasonGuide(
        attempted="A COM call reported a side effect it could not confirm.",
        confirmed="Everything before it is in the COM log.",
        unknown="Whether the unconfirmed stimulus reached the target.",
        physical_check="Check the device behavior against the last confirmed log entries, bring it to a known state, then sign.",
    ),
    "com_reader_audit_broken": _audit_guide("The background COM reader received bytes it could not append to the audit log."),
    "com_write_audit_broken": _audit_guide("A COM write happened (or failed) and its audit entry could not be written."),
    "com_audit_broken": _audit_guide("Stopping a COM session could not write its closing audit entry."),
    "com_report_audit_broken": _audit_guide("A COM call completed but its report could not be persisted."),
    # -- CAN buses --------------------------------------------------------------
    "can_open_interrupted": QuarantineReasonGuide(
        attempted="can_session_start was interrupted while opening the adapter.",
        confirmed="No frame was sent: the session never reached a writable state.",
        unknown="Whether the adapter channel was left initialized and participating on the bus.",
        physical_check="Confirm no process holds the adapter and the bus shows normal traffic (no error flood), then sign.",
    ),
    "can_open_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="can_session_start failed and could not confirm the partially opened adapter was shut down.",
        confirmed="No frame was sent by this session.",
        unknown="Whether the adapter channel is still initialized — a channel brought up at the wrong bitrate disturbs the bus just by listening.",
        physical_check="Confirm the adapter is free and the bus shows normal traffic at the expected bitrate, then sign.",
    ),
    "can_session_setup_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="CAN session setup failed after the adapter opened, and closing the adapter failed too.",
        confirmed="No frame was sent by this session.",
        unknown="Whether the adapter is still open on the bus.",
        physical_check="Confirm the adapter is free and bus traffic is normal, then sign.",
    ),
    "can_send_effect_unconfirmed": QuarantineReasonGuide(
        attempted="can_send raised while transmitting a frame.",
        confirmed="Every earlier frame is in the CAN log.",
        unknown="Whether the frame reached the bus — receivers may have acted on it.",
        physical_check="Check the devices on the bus for the effect of the possibly-sent frame, bring them to a known state, then sign.",
    ),
    "can_read_effect_unconfirmed": QuarantineReasonGuide(
        attempted="can_read raised while receiving.",
        confirmed="Reading transmits nothing; the bus was not stimulated by this call.",
        unknown="Whether the adapter or its bridge process is still in a defined state.",
        physical_check="Confirm the adapter answers again (or replug it) and the bridge process is gone, then sign.",
    ),
    "can_adapter_cleanup_unconfirmed": QuarantineReasonGuide(
        attempted="Stopping a CAN session could not confirm the adapter was shut down.",
        confirmed="The session is out of service; no further frames can be sent through it.",
        unknown="Whether the adapter still participates on the bus.",
        physical_check="Confirm the adapter is free and bus traffic is normal, then sign.",
    ),
    "can_effect_unconfirmed": QuarantineReasonGuide(
        attempted="A CAN call reported a side effect it could not confirm.",
        confirmed="Everything before it is in the CAN log.",
        unknown="Whether the unconfirmed frame reached the bus.",
        physical_check="Check the devices on the bus against the last confirmed log entries, then sign.",
    ),
    "can_queue_clear_audit_broken": _audit_guide("Clearing the CAN receive queue could not be audited."),
    "can_audit_broken": _audit_guide("Stopping a CAN session could not write its closing audit entry."),
    "can_report_audit_broken": _audit_guide("A CAN call completed but its report could not be persisted."),
    # -- Configuration adoption / generation (both callers of the shared read) --
    "config_adopt_discovery_exception": _discovery_exception_guide("project_config_adopt_hardware"),
    "config_create_discovery_exception": _discovery_exception_guide("project_config_create"),
    "config_adopt_discovery_audit_broken": _discovery_audit_guide("project_config_adopt_hardware"),
    "config_create_discovery_audit_broken": _discovery_audit_guide("project_config_create"),
    "config_adopt_terminal_audit_broken": _terminal_audit_guide("project_config_adopt_hardware"),
    "config_create_terminal_audit_broken": _terminal_audit_guide("project_config_create"),
}

_UNKNOWN_REASON_GUIDE = QuarantineReasonGuide(
    attempted="A hardware operation recorded a quarantine reason this build has no catalogue entry for (it may come from a different Agentic HIL version).",
    confirmed="Only what the incident's own records say; read `hardware_lease_status` and `get_last_report`.",
    unknown="What the recording version meant by this reason; treat the device state as unknown.",
    physical_check="Read the failure report the incident references, verify the device against it on the bench, then sign.",
)


def quarantine_reason_details(reasons: list[str]) -> list[JsonObject]:
    """The signer's view of each reason, in the order the reasons occurred.

    Unknown reasons get the explicit fallback rather than nothing: an incident
    persisted by another version still has to be resolvable at this one's CLI.
    """
    return [QUARANTINE_REASON_GUIDES.get(reason, _UNKNOWN_REASON_GUIDE).as_json(reason) for reason in reasons]


def attach_quarantine_guidance(result: JsonObject) -> JsonObject:
    """Attach the per-reason signer guidance to a quarantined result.

    Applied at reporting time, never persisted: records and audit reports keep
    only the reason strings, so catalogue text can improve between versions
    without stale copies surviving in state files."""
    if result.get("quarantined") is not True and result.get("cleanup_required") is not True:
        return result
    listed = result.get("cleanup_reasons")
    reasons = [reason for reason in listed if isinstance(reason, str) and reason] if isinstance(listed, list) else []
    if not reasons or "quarantine_guidance" in result:
        return result
    return {**result, "quarantine_guidance": quarantine_reason_details(reasons)}


# Exactly what each backend reads out of a `debuggers.<name>` entry. "required"
# means the backend cannot work without it; "discovered" means it is found
# automatically when unset; "ignored" means the backend never reads it, so
# setting it changes nothing.
DEBUGGER_FIELD_MATRIX: JsonObject = {
    "openocd": {
        "tool": "openocd",
        "type": {"status": "optional", "value": "openocd", "note": "Default. Omit only if no other backend is meant."},
        "executable": {"status": "discovered", "note": "Falls back to `openocd` on PATH, except on the untouched starter entry, which stays inert until somebody names a toolchain in it. An absolute path or a value containing a separator is resolved against workspace_root and must exist."},
        "probe_id": {"status": "optional", "note": "Adapter serial number, passed as `adapter serial <probe_id>`. Required once more than one debugger is configured."},
        "target_type": {"status": "ignored", "note": "OpenOCD selects the target through target_cfg."},
        "interface": {"status": "ignored", "note": "OpenOCD selects the transport through interface_cfg."},
        "interface_cfg": {"status": "required", "default": "interface/stlink.cfg", "note": "OpenOCD script, passed as `-f`. Once this entry names a toolchain it must be an absolute path to an existing file outside the workspace: the relative default is what OpenOCD's own search path would resolve, and what that resolves to is not this configuration's to promise."},
        "target_cfg": {"status": "required", "default": "target/stm32f4x.cfg", "note": "OpenOCD script, passed as `-f`, absolute and outside the workspace like interface_cfg. Must match the MCU family."},
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
# write `allow_flash: false` on a bench somebody else was about to flash.
#
#   description  what the bench IS   — target, probe identity, port parameters
#   permissions  what the bench MAY  — every permission key in the file: each
#                                      permissions: block, and the two grants
#                                      that sit directly on a section,
#                                      artifacts.allow_upload and
#                                      debug.allow_all_symbols
#
# Neither right reaches upward. `allow_config_permissions_write` opens the
# permissions half in one direction only: `configwrite.permission_widening`
# refuses any write that turns a permission on, so the grant that "hands over the
# granting" hands over the taking-away.
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
# Named here rather than imported: `configwrite` imports this module, so the tool
# whose reach the frozen notice below describes cannot be read back from it.
PROJECT_CONFIG_SET_TOOL = "project_config_set"

CONFIG_RIGHTS: dict[str, str] = {
    CONFIG_DESCRIPTION_RIGHT: (
        "Set the description of the bench field-wise: what hardware is there and how it is reached. "
        "Never a permission, so it cannot touch what may be done to the hardware."
    ),
    CONFIG_PERMISSIONS_RIGHT: (
        "Take permissions away, field-wise: every permissions: block, the two section-level grants "
        "`artifacts.allow_upload` and `debug.allow_all_symbols`, and these two keys themselves. Only "
        "false may be written: `project_config_set` turns no permission on, so this grant reduces "
        "authority and never adds any. Setting it false is the last permission change that tool can make."
    ),
}


def permissions_frozen_notice(closed_key: str, frozen: JsonObject, path: str) -> JsonObject:
    """What the call that closes the permissions grant has to say for itself.

    Said here, in the result of that call, and nowhere else. A reference an agent
    could have read beforehand is not where this belongs: whoever writes
    ``allow_config_permissions_write: false`` loses the way back in the same
    instant, and if the result does not say so, an agent nails the bench shut in
    passing and the operator is in front of a file they have to open by hand —
    the exact state hardci-hq#96 exists to end.

    Three things, because three are what a reader needs: what stands frozen now,
    that the agent itself cannot undo it, and the name of the command that can.

    Said about `project_config_set` and about nothing else, because that is the
    whole of what this closes. Regeneration is a different call under a different
    grant and it is creation rather than a permissions write — the owner's
    clarification on hardci-hq#96 keeps it out of the ratchet deliberately. A
    notice that claimed the whole file was sealed would be describing a rule this
    project does not have.
    """
    return {
        "closed_key": closed_key,
        "irreversible_by_agent": True,
        "frozen_permissions": {name: bool(value) for name, value in sorted(frozen.items())},
        "reopened_by": CONFIG_REOPEN_COMMAND,
        "summary": (
            f"`{closed_key}` is now false, so this was the last permission change `{PROJECT_CONFIG_SET_TOOL}` can make to "
            f"{path}. Every permission listed in `frozen_permissions` stands as it is: the ones still true stay true, "
            "the ones already false stay false, and no further call to that tool can move any of them."
        ),
        "next_steps": [
            "Report this before anything else. The operator has to know the bench's permissions are now fixed, and "
            "which of them were left granted — `frozen_permissions` is that list, read out of the file as written.",
            f"You cannot undo it with `{PROJECT_CONFIG_SET_TOOL}` and there is no permission that would let you. Do not "
            "call it on a permission again.",
            f"A person opens one permission again with `{CONFIG_GRANT_COMMAND} <key>` in the project root — including "
            f"`{CONFIG_GRANT_COMMAND} permissions.{CONFIG_PERMISSIONS_RIGHT}`, which is what unfreezes this — and it "
            f"changes that key and nothing else in the file. `{CONFIG_REOPEN_COMMAND}` is the other way and a much "
            "larger one: it regenerates the whole file from attached hardware, so everything else in it is rewritten "
            "too. Ask for whichever fits rather than looking for a way around this; both are the operator's call, not "
            "yours.",
        ],
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
    # `serial_number` is in the description half for the same reason `probe_id`
    # is: it is what an attached board hands you, and it says which unit this
    # entry is rather than what may be done to it.
    ConfigKeyRule("com_ports", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("device", "baudrate", "serial_number")),
    ConfigKeyRule("can_buses", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT),
    # The permissions half, every block of it.
    ConfigKeyRule("permissions", named=False, under_permissions=False, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("debuggers", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("com_ports", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("can_buses", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    # And the two grants the schema puts directly on a fixed section instead of
    # inside a `permissions` block. A generation writes both true like every
    # other permission, so leaving them out of the key model left two things a
    # generated bench grants that an operator could only take back by opening the
    # YAML — the one thing hardci-hq#96 exists to stop. Only the grant of each
    # section is settable; `debug.allowed_symbols` and `artifacts.allowed_roots`
    # are lists, and this surface writes scalars.
    ConfigKeyRule("debug", named=False, under_permissions=False, right=CONFIG_PERMISSIONS_RIGHT, fields=("allow_all_symbols",)),
    ConfigKeyRule("artifacts", named=False, under_permissions=False, right=CONFIG_PERMISSIONS_RIGHT, fields=("allow_upload",)),
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

# What the schema cannot say about a section: why it is there, and which other
# resource already answers the per-field questions in depth. Keyed by section and
# merged with the schema's own description rather than replacing it — where the
# schema says nothing about a field, this document says nothing either, because
# the alternative is a second description of the same field.
_SECTION_PURPOSE: dict[str, str] = {
    "version": "Which permission model the file is read under. A new file should say `2`.",
    "workspace_root": "The project this configuration authorizes, and nothing else. A server started elsewhere refuses it.",
    "state_root": "Where leases, quarantine incidents and canonical reports live. Outside `workspace_root`, so repository content cannot forge them.",
    "permissions": "What may be done to this project beside its hardware: to this file itself, and to a quarantine incident on this bench.",
    "provenance": "Who wrote this file and who last changed it. A note to a reader; nothing reads it as policy.",
    "target": f"What board this is. Names in reports; `controller` is what a human recognises. Which field actually selects a target per backend, and known-good values: {TARGET_SUPPORT_URI}.",
    "debuggers": f"The debug probes. The entry name is the routing key a test plan addresses. Which of these fields each backend requires, discovers or ignores: {DEBUGGER_BACKENDS_URI}.",
    "debug": "Typed GDB session settings: which symbols may be read and how much.",
    "artifacts": "Which firmware files may be flashed, from where, and how large.",
    "com_ports": "The serial lines. `device` is how a port is opened and `serial_number` is which board it is — name both, because a kernel name like `/dev/ttyACM0` or `COM7` is an enumeration order and moves when another adapter is attached. Reading needs no permission; `assert_dtr`/`assert_rts` decide whether opening one restarts the target.",
    "can_buses": (
        "The CAN buses. `listen_only: true` is how a bus is observed without sending ACK bits, and it is enforced per "
        "adapter rather than assumed: `peak` sets the mode and reads it back from the driver, `socketcan` reads the "
        "kernel's control mode because only `ip link` can set it, and `process` requires the bridge to confirm it. An "
        "adapter that cannot be held to it refuses the session instead of listening anyway. `can_buses_list` reports "
        "`listen_only_enforcement` per bus."
    ),
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
  # Generated true; still true, so the agent may enter what it discovers about
  # the bench and take a permission away when the operator asks for it.
  allow_config_description_write: true
  allow_config_permissions_write: true
  # Taken back: this bench is not to be regenerated from hardware discovery.
  allow_config_write: false
  # Also still true: the agent may clear an incident that names no hardware
  # contact. The ones that need somebody at the board refuse regardless of it.
  allow_recover: true

target:
  name: "thermostat-dut"
  controller: "stm32f446re"

debuggers:
  dut:
    type: "openocd"
    executable: null            # resolved from PATH when this file is loaded
    probe_id: "066AFF495451885087171450"
    # Absolute, and outside workspace_root. An OpenOCD entry that anything can
    # reach — and under version 2 reading reaches every entry — is refused with
    # relative script names: those resolve out of OPENOCD_SCRIPTS and the
    # per-user script directories, which is not a path this file states.
    interface_cfg: "C:/tools/openocd/share/openocd/scripts/interface/stlink.cfg"
    target_cfg: "C:/tools/openocd/share/openocd/scripts/target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      # Both generated false: while either is true, flashing on this probe is
      # refused, and there is no tool here that either one enables.
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
    for bound in ("minimum", "exclusiveMinimum", "maximum", "minLength"):
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

A Nucleo-F446RE on ST-Link, flashed through OpenOCD, talking over the probe's own virtual COM port. It was generated with every permission true but the two that refuse flashing while they are true; what you see beyond those is what the operator asked an agent to take back afterwards — no regeneration of this file from hardware discovery:

```yaml
{CONFIG_WORKED_EXAMPLE}```

`probe_id` is the value nobody can guess and nobody enjoys transcribing: `debugger_probes_list` reads it off the attached probe, and `project_config_set` enters it.

## Changing it over MCP

These calls are the only door. The file itself is protected by deny rules `agentic-hil setup` writes into the host, so an agent's own file tools cannot touch it — that is the precondition for this door, not a contradiction of it.

| Call | Does |
|---|---|
| `project_config_describe` | answers, for **this** configuration in **this** state, which keys you may change right now, which you may not, and which grant would open a locked one. Needs no permission; reading is free. |
| `project_config_set` | sets named keys, field-wise, after checking each value against the schema. |
| `project_config_adopt_hardware` | reads the attached probe and fills in the identity keys that are still unset, through `project_config_set`. Supplies no value of its own. |
| `project_config_create` | regenerates the whole file from hardware discovery when `permissions.allow_config_write` is set. It authors no permission value of its own, but it is a generation and not a narrowing: see below. |
| `project_config_reload_description` | makes a changed **description** the one this running server answers out of, without a restart. Re-reads nothing else. See "Picking up a changed description without a restart". |

### Permissions move one way — through `project_config_set`

A configuration is **generated with every permission true** — flashing, reset, COM and CAN writes, and all three `permissions.allow_config_*` grants — **except `allow_raw_debugger_commands` and `allow_mass_erase`, which are generated false**. Validated flashing and unrestricted debugger access are mutually exclusive, so while either of those is true `flash_firmware` on that probe is refused; neither has a tool behind it here, so leaving them false costs nothing and is what makes the bench flashable (hardci-hq#107). The bench is workable from the moment the file exists, flashing included, and nobody has to open an editor to make it so.

What holds instead of a closed start is the direction of the one call that writes a permission field-wise:

```text
Generation                  every permission true, but the two flashing is interlocked against
Through project_config_set  write false into a permission
                            never true — not even into one you set to false yourself
Last move there             permissions.{CONFIG_PERMISSIONS_RIGHT}: false
After that                  no permission changes through project_config_set at all
Reopened by a person        `{CONFIG_GRANT_COMMAND} <key>` — one named permission, nothing else in the file
```

A change that would turn any permission on is refused as `{CONFIG_WIDENING_ERROR}`, and the check reads the permissions present in the document before and after the write rather than the keys the request named — so there is no spelling of it that gets through. Report the refusal and stop; granting is the operator's.

Closing `permissions.{CONFIG_PERMISSIONS_RIGHT}` freezes the permissions for this call: after it, no permission here can be changed again through `project_config_set`. That call says so in its own result — which permissions stand frozen, that you cannot undo it, and the commands a person reopens it with. Do not make that call in passing.

### What the ratchet does not cover

The two commands a person has are a separate door, and it is honest to say so rather than to promise more than holds:

* `{CONFIG_GRANT_COMMAND} <key>` at a person's shell opens one named permission in the file as it stands and changes nothing else in it; `{CONFIG_REVOKE_COMMAND} <key>` closes one again. That is the surgical reopen path, it is the operator's, and it is the one to name when a permission is what is missing — with the key. Neither is an MCP tool.
* `{CONFIG_REOPEN_COMMAND}` at the same shell rewrites this whole file from attached hardware at the generated defaults. That is the wide reopen path and it is also the operator's; ask for it only when the bench itself has to be rebuilt.
* `project_config_create` over MCP is the same generation under `permissions.allow_config_write`. Entries already in the file keep the permissions this server loaded for them; an entry the discovery finds for the first time arrives at the generated defaults; an entry the discovery no longer finds is dropped; and if the configuration has been deleted in the meantime, the file that comes back is at those same defaults. Its result names what it wrote.
* **"This server loaded" is literal, and it is the sharpest edge of that door.** A server parses the configuration once, at startup, and does not reload. A permission narrowed with `project_config_set` is on disk and is *not* in what the server holds, so a `project_config_create` in that same session writes the older, wider value back. A narrowing binds this path only once the server has been restarted onto the narrowed file — the same restart a `config_stale` result asks for, and that includes closing `permissions.allow_config_write` itself, which is checked against the loaded configuration like every other permission this server enforces. What closes the door for good is an operator setting it false in the file and the server being restarted onto it.
* Anything a person does at the command line, including editing the file, is theirs. Nothing here binds them.

So the claim is exactly this and no larger: **the MCP permission-write path can only narrow.** An operator who wants the whole file to stop moving from the agent side sets `permissions.allow_config_write` to false as well — that closes the regeneration door, and like every other permission here it can be closed from this surface and not reopened from it, taking effect for a running server once it is restarted onto the closed file.

### The two rights

| Grant | Opens |
|---|---|
{rights}

The split is the point. Somebody who opens the file so an agent can enter a probe serial has not, in the same motion and without being told, handed over that agent's ability to narrow the bench underneath somebody else's work.

### Which keys, and what may go in them

| Key | Grant that opens it | Value shape (from the schema) |
|---|---|---|
{keys}

`<name>` is an entry name you choose. A `debuggers`/`com_ports`/`can_buses` entry that does not exist yet is created by setting a key under it — always with every permission false, written by the server. A generation grants; a write only ever takes away, and adding an entry is a write, so a device named this way arrives closed and `{CONFIG_GRANT_COMMAND} <key>` at a person's shell is what opens it, one permission at a time.

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
| turning any permission on through `project_config_set`, with any grant | refused as `{CONFIG_WIDENING_ERROR}`, both for a value other than `false` and for a document that would end up granting more than it did. The permissions actually present in the document are compared before and after the change, so it holds whatever the key was called and whichever grant the caller holds. Regenerating the file is the other door and a different grant — see "What the ratchet does not cover". |
| reaching a permission with only the description grant | refused twice over: the key does not resolve to the description grant, and the same before/after comparison catches a permission that moved without `{CONFIG_PERMISSIONS_RIGHT}`. |
| changing anything while a run is open | a run holds devices under the policy this file states. Changing the policy underneath it is refused; close the run with `bench_run_stop` first. |
| deleting a key, an entry, or the file | nothing on this surface removes configuration. Regenerating one costs an operator their settings. |
| `version`, `workspace_root`, `state_root`, `validation`, `recovery`, and every key of `artifacts` and `debug` except the two grants below | not settable over MCP at all. These decide where trusted state lives, which files may be flashed and how far the machine may recover itself. They are an operator's to write. |
| widening `artifacts.allow_upload` or `debug.allow_all_symbols` | those two are the exception to the row above: a generation grants them like every other permission, so `{CONFIG_PERMISSIONS_RIGHT}` reaches them and `false` is the only value that may be written into either. Their list and path neighbours — `artifacts.allowed_roots`, `debug.allowed_symbols`, `upload_directory` — stay operator-only, so narrowing here is the scalar grant and nothing else. |

A refused write names the grant that is missing and the key in this file that carries it. If the answer is `permission_denied`, that **is** the answer: report it and stop. The configuration belongs to the operator.

## A board plugged in after this file was written

`agentic-hil setup` discovers hardware once. Run with nothing attached, it writes placeholders — `probe_id: null`, `executable: null`, `controller: "unknown-controller"`, no `com_ports` entry — and that is the common case, because installing the tool and connecting the board are two separate moments.

`project_config_adopt_hardware` is the way back in. It reads what is attached and fills in what the file has nothing for.

```json
{{"name": "project_config_adopt_hardware", "arguments": {{"apply": true}}}}
```

| Property | Rule |
|---|---|
| what it carries | `debuggers.<name>.probe_id`, `debuggers.<name>.executable`, `target.controller`, `com_ports.<name>.device`. Identity, and only identity — what an attached probe hands you. |
| where the values come from | hardware discovery on this machine. The arguments *select* (`probe_id`, `debugger_id`, `com_port_id`) and never supply, so nothing of yours can reach the file through it. |
| what counts as unset | absent, `null`, empty, or exactly the placeholder the shipped skeleton writes. Anything else is a value somebody chose: it comes back under `kept`, with what the hardware says beside it, and is not replaced. |
| what it writes through | `project_config_set`, so the same grants, the same schema check, the same validate-before-replace, the same `provenance` record. Without `apply` it writes nothing at all. |
| permissions | it cannot name one. `permissions_changed` in the result is the file's own before/after answer, not a claim. |
| more than one probe attached | `ambiguous_hardware`, listing every attached serial. Name one as `probe_id`. It never chooses a board. |
| nothing attached, or no port carrying the probe's serial | said as such, with the host's serial ports listed, rather than guessed. |
| a probe already named, and a different one attached | `hardware_mismatch`, and nothing is planned. The keys describe one board between them, so carrying only the unset ones would leave a `probe_id` naming one Nucleo beside another's controller and COM port. |
| the board is busy | reading a probe takes the same machine-wide lock every hardware call takes, so a board another server, run or terminal is holding answers `device_busy` and nothing is read. |
| version 1 configurations | reading a probe there still needs `allow_probe` on the entry, and this is a probe read: `permission_denied` if it is false, exactly as `probe_target` answers. |
| refused | `permission_denied` on `{CONFIG_DESCRIPTION_RIGHT}` still returns the plan. Report the keys and values it names and let the operator run `agentic-hil adopt-hardware`. |

## Picking up a changed description without a restart

A server parses this file once and enforces that document until it exits. That rule is about the **permissions**: they were taken as a whole, and a document that turned up underneath a running server may not widen them. It used to hold the **description** hostage too — a board plugged in and written down after the server started was invisible to it, and the only way across was a restart of the agent's MCP server.

`project_config_reload_description` is the way across. It takes no arguments.

```json
{{"name": "project_config_reload_description", "arguments": {{}}}}
```

| Property | Rule |
|---|---|
| what it re-reads | `target`, `debuggers`, `com_ports`, `can_buses` — minus each entry's `permissions:` block. That is the whole list. |
| what it does not | **every permission, in either direction.** Not narrowed, not compared, not adopted. The grants in force after a reload are byte for byte the ones parsed at startup. |
| a device that is new to this server | arrives with **no grant at all**. It can be probed and read — from `version: 2` on, reading needs no grant; exclusivity is what protects it — and flashing, reset, mass erase and COM/CAN writes are denied on it until an operator restarts the server onto the file that grants them. |
| a device renamed on disk | the new name is a device this server has never seen, so it arrives closed, and the old name is gone. Renaming an entry costs its grants until a restart. |
| which board is bound | the name this server already drives, wherever it still exists. A second entry appearing does not repoint a bound server; a bench that configured no debugger at all binds the first one that appears, because there is exactly one board it could mean. |
| refused while | a run, a COM/CAN session or a debug session holds this bench (`config_reload_in_open_run`), or an incident on it is unresolved (`resource_quarantined`). Both because a held name has to keep meaning the same physical board. |
| refused when | the file is missing, unreadable, or does not load — `config_file_not_found`, `config_unreadable`, `config_invalid`, the same three states `config_status` reports. Nothing changes on any of them. |
| what it writes | nothing. It reads the file, so no grant gates it; a bench whose `permissions.{CONFIG_DESCRIPTION_RIGHT}` is false can still pick up a board somebody plugged in. |

### What still needs a restart, by name

These are refused by name rather than quietly skipped, because each of them is either a permission wearing a description key's clothes or a section that mixes the two, and guessing one into the description half would make this a general reload:

| Not re-read | Why |
|---|---|
| `permissions`, and every `<section>.<entry>.permissions` | the grants. The whole point. |
| `version` | decides which permission model the file is read under. Moving 1 → 2 makes every read on this bench free, which is a permission change. |
| `workspace_root`, `state_root` | what this server is bound to and where its trusted state lives. A reload may not rebind a running server or orphan its leases. |
| `debug` | `allow_all_symbols` is a grant and `allowed_symbols` is an allowlist over what may be read out of a target. |
| `artifacts` | `allow_upload` is a grant and `allowed_roots` decides which files may be flashed. |
| `validation` | decides which artifacts are accepted at all. |
| `recovery` | decides whether this machine may drive a physical reset on its own. |
| `reports`, `logs` | operator-owned paths; a running server's audit trail may not move underneath it. |

### After a reload

`config_status` compares the file against the description now in force, so a reload that just took the file's description does **not** leave `config_stale: true` behind for it. What does not disappear is the other half: when the file's permissions differ from the ones being enforced, the status carries `permissions_source`, which says the grants came from the document parsed at startup and that a restart is what adopts the file's. The reload's own result lists them under `permission_differences`, taken at the moment both documents were in hand.

At a shell the same operation is `agentic-hil config-reload`, which loads this file the way a server does and reports what a running server's reload would take from it and what it would leave — the pre-flight for asking an agent to make the call.

## Getting from "board attached" to a valid change

1. `project_config_describe` — what may this caller change right now.
2. `project_config_adopt_hardware` — what is attached, and which keys it would fill in. Nothing is written yet.
3. The same call with `{{"apply": true}}`, or `project_config_set` for a key it left alone.
4. `project_config_reload_description` — make what was just written the description this server answers out of. The write's `reload_required` is about that, and this is what clears it for the four device sections. A permission the write changed still waits for a restart.
"""


LEASE_LIFECYCLE_DOCUMENT = """# Device exclusivity, hardware leases, and the quarantine lifecycle

Two different things guard the hardware, and they live in two different places.

**Exclusivity** is machine-wide and keyed on the physical device. It is what replaced the read permission: reading needs no grant, because whoever holds a board cannot be disturbed and whoever reads while no run holds it disturbs nobody.

**The lease** is per configuration, lives under `state_root`, and records what a call did and whether the hardware was left in a confirmed state. It survives process exit; that is what makes an abandoned incident visible instead of forgotten.

A third thing *describes* the hardware contact and guards nothing. Every tool in `tools/list` carries MCP `annotations` — `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` — set from what the tool demonstrably does, on the same line this document already draws for hardware contact. `project_config_reload_description`, `com_read`, `can_read`, `bench_run_status` and `project_config_describe` change nothing and say so; `flash_firmware` and `debug_start_session` — whose `load` mode programs flash, which is why it needs `allow_flash` — are declared destructive; `probe_target` is deliberately *not* read-only, because an SWD attach halts the core. They exist so a host can tell a harmless call from an irreversible one instead of judging by the tool's name. They are hints in the protocol's sense and this server enforces nothing by them: what a call may do is decided by the configuration's permissions and by the exclusivity below, exactly as before. A `readOnlyHint: true` is not a grant, and a `destructiveHint: false` is not a promise that a call will be allowed.

## Device exclusivity

| Property | Rule |
|---|---|
| what is locked | the physical device: `physical:<resource_id>`, `probe:<serial>`, `probe-exe:<executable>`, `com:serial:<serial_number>`, `com:<device>`, `can:<adapter>:<channel>` |
| case | a name for hardware — `resource_id`, a probe serial, a port's `serial_number`, a CAN channel — folds case on every platform, because `0669FF` and `0669ff` are one unit wherever the bench runs. A host path — a debugger executable, a serial device — folds the way its own filesystem does, so `COM7` and `com7` are one port on Windows while `/dev/ttyACM0` and `/dev/ttyacm0` are two on Linux. Two entries whose `resource_id` values differ only in case are refused at config load rather than merged |
| a serial port's identity | `com_ports.<name>.serial_number` is the adapter's USB serial and is what the lock follows; `device` is only how the port is opened. Without it the key falls back to the device name, which is an enumeration order — attaching a second adapter can hand one entry another board — so an entry that names neither a `serial_number` nor a `resource_id` carries an `identity_warning` saying so. An entry that *does* name hardware is opened on one ground only: the attached device is `confirmed` to be the board it names. A port that has come to be a different board is refused with `com_port_identity_mismatch`; a port whose identity cannot be checked at all — no serial backend, not enumerated exactly once, or no serial reported — is refused with `com_port_identity_unverified`, because a check that could not run does not prove the name still leads to its board. Both refuse before the port is opened and are retry-safe. An entry that names no hardware is opened as `not_declared`, unverified by design |
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

What `listen_only: true` is worth is what the adapter can be held to, and that differs by adapter, so it is enforced rather than assumed (hardci-hq#103).

| adapter | how the mode is obtained | what backs the claim |
|---|---|---|
| `peak` | set through PCAN's `BusState.PASSIVE`, re-asserted once the channel is initialized | `PCAN_LISTEN_ONLY` is read back from the driver; a channel that does not read back listen-only is closed and the session refused |
| `socketcan` | not obtainable from this process: the mode belongs to the kernel's CAN device and only `ip link` sets it | the control mode is read before the socket is created; an interface not already in listen-only refuses the session |
| `process` | sent to the bridge in its `open` request | the bridge's `open` result must answer `listen_only: true`; forwarding alone confirms nothing |

An adapter that cannot be held to it refuses with `can_listen_only_unsupported` (before the bus is touched, `retry_safe`) or `can_listen_only_unconfirmed` (asked, not confirmed, adapter closed again). Neither downgrades to listening anyway. `can_buses_list` reports `listen_only` and `listen_only_enforcement` per bus, so the scope is readable before a session is started.

What none of this proves is what the silicon does. The claim reaches as far as the driver's or the kernel's own report of its mode; the bench proof is a single sender with no other participant reporting its frame unacknowledged, and that stays with the operator.

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

## What quarantines, and what merely refuses

Quarantine answers one question — "is the physical state of the hardware unknown?" — never the weaker "did something go wrong?". A failure that can prove it never reached the hardware refuses with a named error and `retry_safe: true`, leaves no quarantine record, and keeps the bench in service; the board is exactly as the last call that did reach it left it.

| Failure | Outcome |
|---|---|
| toolchain executable not found (OpenOCD, pyOCD, STM32CubeProgrammer CLI, the debug server, GDB) | refusal — no process ever existed |
| OpenOCD rejected the command before `init`, or a `-f` script failed to load, or the adapter could not be opened, or no target answered, with the init-stage marker absent | refusal — `init` never completed, so nothing was brought under debug control |
| pyOCD found no probe / could not open it, refused the configured `target_type`, or reported its connect sequence failed | refusal — no core came under debug control |
| ST-Link probe absent (`no ST-LINK detected`) or no target behind it (`No STM32 target found`) | refusal — the channel carried nothing |
| `probe_target` / `debugger_probes_list` failed and the backend named no abort point — a timeout that killed it mid-call (`timeout`), an exit whose output does not carry the confirmation the tool asks for (`target_state_unconfirmed`) | **quarantine** (`debugger_readonly_target_state_unconfirmed`) — being read-only is not being passive: an SWD attach halts the core, and a killed process never ran its own `shutdown`. Settled by a verified reset-into-halt, never by a re-read. The `error_type` is its own, never `target_not_detected`: that one is the backend's report that the adapter was reached and nothing answered, which is a row above and refuses |
| COM port could not be opened and the handle is verifiably closed | refusal — the port never carried a byte of the session |
| CAN adapter never initialized on SocketCAN (python-can `CanInitializationError`) | refusal — `SocketcanBus()` only creates and binds a socket; the controller is brought up out of band |
| CAN adapter never initialized on PCAN (`PcanCanInitializationError`) | **quarantine** — python-can raises it from four `SetValue` calls that run after `PCANBasic.Initialize` succeeded, and the class carries no phase marker, so an initialized channel that is already ACKing on the bus looks the same |
| a direct CAN read failed | refusal — `recv()` transmits nothing |
| anything whose abort point cannot be proven — an unconfirmed flash or reset, an exception mid-call, an unconfirmed cleanup, an owner that died holding a lease, a broken audit trail | quarantine; that is the feature |

Between the two sits the self-resolving class: a reason the next hardware call can settle itself under `recovery.auto_recover` (a read-only re-read, or a verified reset-into-halt) is cleared by the machine, without an operator.

## What a justified quarantine tells the signer

`recover --confirm-safe-state` is a signature, so every quarantined result and `hardware_lease_status` carry `quarantine_guidance`: one entry per `cleanup_reason` with `attempted` (what was being done when confirmation was lost), `confirmed` (what still holds), `unknown` (the exact gap that makes a machine answer impossible), and `physical_check` (what to verify on the physical board before signing). An incident recorded by another version gets an explicit fallback entry rather than silence.

## When a call is refused with `resource_quarantined`

1. Stop every effect. Do not retry around it, and never delete state under `state_root`.
2. Read `hardware_lease_status` (CLI: `agentic-hil lease-status`). It reports `cleanup_reasons`, `quarantine_guidance`, `auto_recoverable`, `auto_recover_policy`, and the current `quarantine_id`.
3. Branch on `auto_recoverable`.

| Field | Value | Do |
|---|---|---|
| `auto_recoverable` | `true` | Retry the hardware call **once**. The running owner reaps its leftover debugger processes, verifies the safe state under the bench policy, and proceeds. |
| `auto_recovery_attempted` in the refusal | `true` | That already ran and did not confirm the safe state. Do not retry again; go to the operator path. |
| `auto_recoverable` | `false` | Operator path. |

## Operator path

The operator performs the `physical_check` each `quarantine_guidance` entry names, confirms the bench matches, then:

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


PLATFORM_PATHS_DOCUMENT = """# Where each file lives, and which command touches it

Agentic HIL keeps three things outside the workspace: the authoritative configuration, `state_root`, and the machine-wide device locks. This is where each of them goes on each platform, and how to move the first two when the default location does not suit the machine.

## What is checked about a path, and what is not

A configured path is opened component by component without following links, and every component of the chain is held open while the operation runs — on Windows without `FILE_SHARE_DELETE`, which blocks a rename or a delete of any of them for the duration. So a path is refused when it *is not what it claims to be*: a symlinked component, a file where a directory is needed, a final object that is not a single-link regular file. That refusal carries `error_type: unsafe_configured_path` and names the component that stopped the walk.

What is **not** asked is who else on this machine could write the path. A Windows ACL walk and a POSIX mode/sticky-bit walk over every ancestor used to ask exactly that, and both were removed in 0.8.0 (hardci-hq#95). Two reasons, and the second is the one that decides it:

- A multi-user guarantee was never a requirement of this project. The check only ever defended against a *different account on the same machine*.
- It could not have held one anyway. The operator owns these objects and holds FullControl on them, so every ordinary process of that user can rewrite the configuration with a text editor, whatever any ACL says.

What replaces it is detection rather than prevention, and it was already there:

- **The digest.** Every tool result carries `config_status`, which says whether the configuration on disk is still byte-for-byte the one this server loaded. Platform-neutral, one SHA-256, and it can lock nobody out. `debugger_info`, `project_config_describe` and `agentic-hil doctor` carry it in every answer, so "this is the configuration in force" is a positive statement rather than an absence.
- **The audit trail.** Every hardware action records the digest of the configuration it ran under.
- **The file-level deny rules.** `agentic-hil setup --agent <agent>` writes rules into the agent's own configuration that refuse that agent's file tools on the authoritative configuration. That is a different mechanism and it is untouched: it is the one place an agent is kept away from the policy file.

The honest loss: detection comes after the fact. Prevention here was largely imagined, because the same user is always allowed to write.

## Which command touches which of these

User scope is per user, per machine: all of it under the invoking user's home, invisible to other OS users on the host.

| Command | Scope | Touches |
|---|---|---|
| `agentic-hil agent-install --agent <agent>` | user, once per user and agent | the agent's skill directory and its user-level MCP config, both under the home directory; checks that a persistent executable exists to register |
| `agentic-hil init [--agent <agent>]` | project, once per workspace | the authoritative configuration and `state_root`, then `doctor` |
| `agentic-hil setup --agent <agent>` | both, in order | both of the above |

Each half owns its rollback set. A project step that fails leaves the installed skill and the MCP registration standing, and `agentic-hil init` alone is what re-runs.

## Where things go

| Item | Windows | POSIX |
|---|---|---|
| authoritative configuration | `%APPDATA%\\agentic-hil\\projects\\<name>-<digest>\\config.yaml` | `$XDG_CONFIG_HOME/agentic-hil/projects/<name>-<digest>/config.yaml` |
| `state_root` | `%LOCALAPPDATA%\\agentic-hil` | `$XDG_STATE_HOME/agentic-hil` |
| device locks | `%USERPROFILE%\\.agentic-hil\\device-locks`, fixed | `~/.agentic-hil/device-locks`, fixed |

The device lock directory is not configurable and has no environment override. It is the one place every process on this machine agrees to look for who holds a board, and an override is how two sessions stop seeing each other — which is the failure it exists to prevent. The home directory is chosen because every process of one user reaches it without having to agree on a configuration first.

## Choosing another location

`AGENTIC_HIL_CONFIG` and a freely chosen `state_root` are not debug switches. They are how a project binds to a configuration and a state directory the discovered defaults do not cover: a redirected profile directory, a roaming share, a volume the operator would rather keep this off.

```text
AGENTIC_HIL_CONFIG=C:\\Users\\<user>\\.agentic-hil\\projects\\<workspace-name>\\config.yaml

# inside that config.yaml
state_root: C:\\Users\\<user>\\.agentic-hil\\state
```

Using them costs nothing else: `state_root` has no fixed location beyond being absolute and not overlapping `workspace_root`, and a configuration selected by `AGENTIC_HIL_CONFIG` is read exactly like a discovered one — same schema, same validation, same permissions. Nothing about a project is second class for having taken this route.

The one rule that does not bend: set `AGENTIC_HIL_CONFIG` in the host's user-level, managed, or parent-process environment, **never** in a repository-controlled file (`.vscode/mcp.json`, `.mcp.json`, `.codex/config.toml`, `opencode.json`). An agent that can edit the file that selects the configuration can select a configuration it wrote.

Rules that hold on both platforms:

- `AGENTIC_HIL_CONFIG` is optional and must be an absolute path to the configuration file.
- `workspace_root` and `state_root` are both mandatory and absolute, and must not overlap in either direction.
- The discovered default configuration path is derived from the workspace path, so it is canonical per workspace; a config found elsewhere is only accepted through `AGENTIC_HIL_CONFIG`.
- Whether an agent may write the configuration is decided by the configuration, in `permissions.allow_config_write`, and by nothing else. There is no second state store: what holds is what a person reads in the file. A workspace with no configuration lets an agent generate one, and a configuration deleted out of band lets it generate a fresh one — the generated skeleton again, at the generated defaults, so the round trip discards every narrowing the operator had asked for and produces the file `agentic-hil init` would have written.
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
        "Where each file lives on each platform",
        "Where the authoritative configuration, state_root and the machine-wide device locks go on Windows and POSIX, which command touches which of them, what is and is not checked about a configured path, and how AGENTIC_HIL_CONFIG plus a chosen state_root bind a project to another location.",
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
