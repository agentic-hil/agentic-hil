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
TEST_PLAN_URI = f"{RESOURCE_SCHEME}://reference/test-plan"
TEST_PLAN_SCHEMA_URI = f"{RESOURCE_SCHEME}://reference/test-plan-schema"

ERROR_URI_PREFIX = f"{ERRORS_URI}/"
DEBUGGER_BACKEND_URI_PREFIX = f"{DEBUGGER_BACKENDS_URI}/"

JSON_MIME = "application/json"
MARKDOWN_MIME = "text/markdown"

BACKENDS = ("openocd", "stlink", "pyocd")

# The plan the reactor reads when nobody names another one, and the packaged
# schema every plan is validated against. Both live here rather than in
# `test_reactor`, because the reference document below is generated from them
# and that module imports this one: a second copy of either would be exactly the
# drift this file exists to prevent.
DEFAULT_TEST_CONFIG_PATH = ".agentic-hil/testconfig.yaml"
TEST_CONFIG_SCHEMA_RESOURCE = "schemas/testconfig.schema.json"
# The annotation the plan schema marks a newer format's additions with. A plan
# is held to what its own `version:` contains, and the schema is what says which
# version each action and each key arrived in.
PLAN_FEATURE_VERSION_KEY = "x-since-version"
# The keys a step names its device with: the one routing key from version 3 on,
# and the version 2 aliases that spell the section out. The reactor builds the
# same tuple from its device classes (`test_reactor.ROUTE_FIELDS`), which this
# module cannot read because that module imports this one. A test holds the two
# equal, so a new device kind cannot quietly leave this document describing a
# routing surface the reactor no longer has.
PLAN_ROUTE_KEYS: tuple[str, ...] = ("device", "debugger", "port_id", "bus_id")

# The loaded configuration and the file it came from have come apart. Not a
# refusal (every tool still works) but it is carried like one, because what a
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
# And the surgical one beside it. `init --force` returns the whole
# file to the generated defaults by rewriting it from hardware discovery, which on a
# bench somebody grew is not a repair but a loss: baudrate, `resource_id`,
# `state_root` and the artifact roots go with it. These two name one permission
# and move that one. Both directions ship together on purpose: a command that
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
# The rendering refusing to publish a result it cannot vouch for. Every result
# rendered as prose is redacted first, and a redaction that hands back something
# other than a document leaves nothing standing behind that claim, so the
# command line prints this in place of the result. Nothing on the bench failed,
# which is why it is its own error_type rather than one of the failures above.
REDACTION_UNAVAILABLE_ERROR = "redaction_unavailable"

# Both of these act on flash outside the path this server validates (a raw
# debugger command writes whatever it is given, a mass erase clears whatever a
# flash has just written), so once either is allowed, a flash report's claim
# about what is on the device is no longer one this server can stand behind.
# That is what makes validated flashing and unrestricted debugger access
# mutually exclusive policies (docs/security-design.md): while either of these is
# true on a probe, `flash_firmware` on that probe is refused, and so is a debug
# session.
#
# They are therefore the two exceptions to the allow-by-default generation, and
# a generated configuration writes both false. For a while it did not, and the
# two rules met for the first time on the bench: the generation opened every
# permission, this interlock reads both of these as a reason to refuse, and the
# result was that a freshly generated configuration could not flash at all. The
# interlock was written when the pair defaulted to false, so "both on" meant
# somebody had chosen it; the inversion made it the shipped state without
# either rule changing.
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
# hardware-discovery path and `project_config_create` cannot drift apart again,
# which is exactly how they came to disagree in the first place.
EXCLUSIVE_FLASH_PERMISSIONS = ("allow_raw_debugger_commands", "allow_mass_erase")

# `can_buses.<name>.listen_only: true` is the one configuration flag whose entire
# value is that it is a proof rather than a preference, so the two ways it can
# fail to be one are named separately. `unsupported` is settled
# before the bus is touched: the adapter has no such mode on this host, so the
# refusal is clean and `retry_safe`. `unconfirmed` is settled after: the adapter
# was asked, was already on the bus by the time it could answer, and did not
# confirm; it is closed again and the exposure named. Silently downgrading either
# to "listening anyway" is the defect these exist to prevent.
LISTEN_ONLY_UNSUPPORTED_ERROR = "can_listen_only_unsupported"
LISTEN_ONLY_UNCONFIRMED_ERROR = "can_listen_only_unconfirmed"
# A transmit asked for on a bus configured `listen_only: true`. Not a shade of
# `permission_denied`, and the distinction is the whole point: permission is
# about what this caller may do on a bus that can carry the frame, while this is
# about a bus that was declared to carry none. So the mode is settled first and
# `allow_write` never gets to speak: a bus is not made transmit-capable by
# granting a permission on it, and answering "denied" would have invited exactly
# that fix. The two gates coexist in that order and only in that order.
LISTEN_ONLY_MODE_ERROR = "can_listen_only_mode"
# A SocketCAN channel that is not a netdev on this host. Its own error_type
# rather than a shade of `can_adapter_open_failed`, because it is the one open
# failure whose outcome is not merely unproven: the bind had nothing to bind to,
# so no controller was addressed and the bench is untouched.
CAN_INTERFACE_NOT_FOUND_ERROR = "can_interface_not_found"
# The vendor library a configured CAN adapter is driven through is not installed
# on this host. The same class of claim as the two above and the strongest of
# them: python-can raises before a driver object exists, so there was nothing for
# contact to happen through. A host-setup refusal, not a quarantine.
CAN_ADAPTER_LIBRARY_MISSING_ERROR = "can_adapter_library_missing"
# A PCAN channel the driver does not have. Provable innocence for the same reason
# a missing SocketCAN netdev is: `PCANBasic.Initialize` answered that the handle
# is invalid, so no channel was opened, nothing was put on any bus, and no
# controller ACKed. Deliberately distinct from the four `SetValue` failures that
# run *after* a successful `Initialize`, which leave a channel that is on the bus
# and keep the quarantine they earn.
CAN_CHANNEL_NOT_AVAILABLE_ERROR = "can_channel_not_available"
# A remote frame asked for on a bus configured `fd: true`. Not a variant of
# `invalid_argument`: CAN FD's FDF bit sits in the position classic CAN's RTR bit
# held, so an FD controller has no remote frame to send at all, and the request is
# refused before it is built into anything rather than sent as whatever an FD
# frame with a stale RTR bit would come out as.
CAN_FD_REMOTE_FRAME_ERROR = "can_fd_remote_frame_unsupported"
# A payload longer than eight bytes on a bus that is not `fd: true`. Its own
# error_type rather than the plain `invalid_argument` a payload over
# `max_frame_data_bytes` gets, because raising that configured ceiling cannot fix
# this one: classic CAN's data field is eight bytes full stop, on every
# controller, and the schema's own `max_frame_data_bytes` maximum used to allow a
# non-FD bus to be configured past it.
CAN_CLASSIC_FRAME_TOO_LARGE_ERROR = "can_classic_frame_too_large"
# A payload on an `fd: true` bus whose length is not one of the sixteen a CAN FD
# DLC field can encode. Above eight bytes the encoding stops counting one at a
# time and jumps by fours, then by eights, then by sixteens, so a length between
# two of those steps cannot be put in a real frame. There is no padding this
# server invents on a caller's behalf, because padding silently sent is data on
# the wire the caller did not ask for.
CAN_FD_FRAME_LENGTH_INVALID_ERROR = "can_fd_frame_length_invalid"
# A serial device another program is already holding. Its own error_type for the
# same reason as the one above: the open was refused by the operating system
# before this session had a handle, so the port kept whatever the other holder is
# doing with it and this bench was not touched. It also answers a question
# `com_port_open_failed` cannot: that the remedy is a process on this host and
# not a cable, a driver or a configuration entry.
COM_PORT_BUSY_ERROR = "com_port_busy"
# What `hardware_recover` answers when the incident needs somebody at the bench
#. Not `permission_denied`: no grant on this or any bench opens
# it, because the missing thing is a statement about a physical board and not an
# What `hardware_recover` answers when the incident needs somebody at the
# bench. Not `permission_denied`: no grant on this or any bench opens it,
# because the missing thing is a statement about a physical board and not an
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
    programmer their board happens to use.

    It gives the reason rather than restating the rule. "Mutually exclusive
    policies" on its own reads as an arbitrary interlock, and an operator who
    takes it that way reaches for the flag it names, which is the one move that
    keeps flashing refused."""
    entry = f"debuggers.{debugger_id or '<name>'}.permissions.{blocking}"
    return (
        f"{action} is disabled while {blocking.removeprefix('allow_')} is allowed on this probe: it acts on flash "
        f"outside the path this server validates, so while it is allowed a flash report's claim about what is on the "
        f"device is no longer one this server can stand behind. That is what makes validated flashing and unrestricted "
        f"debugger access mutually exclusive policies, and why a generated configuration leaves both false. Something "
        f"on this bench set `{entry}` to true since. Set it back to false (with `project_config_set`, or by asking "
        "the operator) and this works. Nothing here can set it back to true afterwards, and no tool here is behind "
        "that flag, so nothing becomes unavailable by turning it off."
    )
# The scope that separates "this project has no configuration", which
# `project_config_create` answers, from "the configuration this running server
# loaded is gone from disk", which it must not.
CONFIG_RUNNING_SERVER_SCOPE = "running_server"
# The scope that separates the two states a missing GDB can be in, because they
# are not one question and do not have one answer. A bench that never named a
# GDB and had none to find is scoped here; a `debug.gdb_executable` somebody
# wrote that no longer resolves keeps the unscoped entry.
GDB_NOT_CONFIGURED_SCOPE = "not_configured"
# The third state, which is neither. The document named no GDB, so
# `project_config_describe` reports the key unset, but startup did autodetect one
# and pinned its path, and that path has since gone. It is not the unscoped
# entry, which would send an operator to correct a value nobody wrote, and not
# `not_configured`, which says nothing was ever found. This scope carries the
# remediation for a GDB nobody configured that was there and is not now.
GDB_AUTODETECTED_MISSING_SCOPE = "autodetected_missing"
# Said by `doctor`, which parses the file at the moment it is asked and is
# therefore always current, which is exactly why it cannot speak for a server
# that has been running since before the last edit. It names both ways across,
# because since `project_config_reload_description` a restart is no longer the
# only one, and this is the last place that said it was.
RUNNING_SERVER_COMPARISON = (
    "This is the configuration as it is on disk right now; the command line reads it fresh on every invocation. A "
    "running MCP server does not: it keeps serving what it parsed at startup. Compare the `loaded_digest` here with "
    "`config_status.loaded_digest` in that server's `debugger_info`. If they differ, that server is enforcing an older "
    "configuration. Two things change that, and which one depends on what moved: `project_config_reload_description` "
    "on that server adopts a changed `target`, `debuggers`, `com_ports` or `can_buses` without a restart, and a "
    "restart adopts everything else: every `permissions:` block included, which that call never re-reads in either "
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

    ``cli_remediation`` is the same steps in the order a person who typed a
    command reads them, and is set only where the two readers of one refusal
    have different first moves. It is a reordering and never a second text: the
    steps are the same objects, so a step edited once is edited for both, and a
    step added to one ordering and forgotten in the other is caught by the guard
    over this catalogue rather than shipped as advice one reader never sees.
    ``remediation`` stays the ordering an agent over MCP reads, which is who
    most refusals on this surface reach.
    """

    meaning: str
    remediation: tuple[str, ...]
    do_not: tuple[str, ...] = ()
    cli_remediation: tuple[str, ...] = ()

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
    put when the discovered default cannot be used: a redirected profile
    directory, a roaming share, a location the operator would rather not use.
    """
    return str(_home() / ".agentic-hil")


def safe_state_root_suggestion() -> str:
    return str(Path(safe_user_root()) / "state")


def safe_user_config_suggestion() -> str:
    return str(Path(safe_user_root()) / "projects" / "<name>-<digest>" / "config.yaml")


def _substitutions() -> dict[str, str]:
    return {
        "safe_user_root": safe_user_root(),
        "safe_state_root": safe_state_root_suggestion(),
        "safe_user_config": safe_user_config_suggestion(),
        "reopen_command": CONFIG_REOPEN_COMMAND,
        "grant_command": CONFIG_GRANT_COMMAND,
        "test_plan_reference": TEST_PLAN_URI,
    }


# The one thing that fixes a proxy neither the manager's roots nor this machine's
# store trusts. Standing text, which is why it lives here rather than beside its
# one caller: `upgrade.py` attaches it as a case-specific `next_steps` entry on the
# runs whose own words name a trust failure this machine's store did not answer,
# and the same string reaches `installation_broken` and
# `installation_changed_after_failed_upgrade`, whose own catalogue entries say
# nothing about certificates. It is deliberately not a standing `upgrade_failed`
# remediation step: a run that got past the proxy and then failed for a reason of
# its own carries `certificates` too, so an unconditional CA imperative would
# contradict the very result that says the store already worked. The catalogue
# points at these measured steps through its conditional `certificates` clause
# instead.
#
# Named as a step and never performed: it writes to a trust store, which is the
# operator's, and the alternative an impatient reader reaches for is a switch
# that turns verification off, which this project offers nowhere.
INSTALL_THE_PROXY_CA = (
    "Install the proxy's own CA certificate into this machine's certificate store. Until that is done there is "
    "nothing this command can be pointed at that trusts what the proxy presents, and no switch that turns "
    "verification off is a fallback. TROUBLESHOOTING.md section 1 is the rest of it."
)

# The four steps out of a workspace with no configuration, named here because
# two readers meet them in different orders. `config_file_not_found` is the one
# refusal on this surface that reaches both: an agent whose first tool call hits
# it, and a person whose first `agentic-hil doctor` hits it before anything has
# been set up. Each has a route and neither has the other's, so the entry below
# carries one ordering for each and these are the steps both orderings are made
# of. Written so that either route reads correctly first or second: neither
# opens by referring to the other.
_CONFIG_MISSING_MCP_ROUTE = (
    "Over MCP, call `project_config_create` once. It takes no arguments and writes this workspace's authoritative "
    "configuration out of the hardware attached to this machine."
)
_CONFIG_MISSING_SHELL_ROUTE = (
    "At a shell, run `agentic-hil init` from the project root; it writes that same file, and it is the operator's own "
    "route rather than the agent's. When the agent on this machine is yours to register as well, `agentic-hil setup "
    "--agent <claude-code|codex|opencode>` installs that agent's skill, registers the MCP server and runs this same "
    "project half in one command."
)
_CONFIG_MISSING_WHAT_IT_WRITES = (
    "Over MCP, `project_config_create` writes every permission true except `allow_raw_debugger_commands` and "
    "`allow_mass_erase`, which it writes false so that flashing works, so the bench is workable from the file it "
    "produces without anyone editing YAML; over that route the two flash interlocks are false by construction. "
    "`agentic-hil init` instead starts from the project's `agentic-hil.config.example.yaml` and honours whatever "
    "permission that file names, so it can hand back a deliberately narrower bench, or a wider one where that file "
    "opens a flash interlock: the interlocks come back false only when the example leaves them so. Read the "
    "`permissions` and `narrowed_permissions` it reports rather than assuming the open defaults, and if either "
    "`allow_raw_debugger_commands` or `allow_mass_erase` comes back true say so, `allow_mass_erase` above all."
)
_CONFIG_MISSING_REPORT_AND_ASK = (
    "The permissions in it are the operator's to narrow, so an agent reports what it granted (flashing and resetting "
    "among them) and asks the operator which of those this bench should not have. `project_config_set` writes `false` "
    "into any of them and never `true`. No tool here reads raw debugger commands or mass erase as permission to do "
    "anything, so wherever the generated file leaves them false they are a closed default rather than a grant gone "
    "missing."
)

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
            "and nothing more: not what the file now contains, and not what a restart onto it would produce."
        ),
        remediation=(
            "If what changed is the description of the bench (`target`, a `debuggers`, `com_ports` or `can_buses` "
            "entry, a probe id, a COM device, a baudrate), call `project_config_reload_description`. It re-reads those "
            "four sections and clears this, without a restart and without touching a single permission. Its result "
            "names anything in the file it did not take.",
            "Otherwise, ask the operator to restart the MCP server, then repeat the call. Say which server: the one "
            "the agent host started for this workspace, not the `agentic-hil` command line, which reads the file fresh "
            "every time and is already current. A restart is what adopts a changed permission, a changed `version`, "
            "and every section outside those four.",
            "If the restart does not come up, the startup refusal names what is wrong with the file. That is the "
            "message to report, and repairing the file is what the restart was waiting for. `agentic-hil doctor` "
            "reads the file fresh and produces the same refusal without stopping anything.",
            "Until one of the two has happened, treat what this server says about the bench as the older file's "
            "answer. Do not reconcile the difference by guessing which of the two is right: `config_status.path` "
            "names the file and `loaded_digest` versus `current_digest` says they differ.",
            "If the change was made through `project_config_set` or `project_config_create` in this session, this is "
            "the same fact those results reported as `reload_required`, not a second problem.",
        ),
        do_not=(
            "Do not carry on flashing, resetting or driving devices on the assumption that the new file is in force. "
            "It is not, and the permissions in force are the older ones, which may be wider than what the operator "
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
            "permissions it loaded even when the file is gone, so that call is refused, and if it were not, it would "
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
            "Report `config_status.backend_error`: it names what the read failed on (a permission on the file or a "
            "directory above it, a path component that is no longer a directory, bytes that are no longer UTF-8).",
            "Ask the operator to make the file readable again and restart the MCP server, so that what is enforced and "
            "what can be read are the same document.",
        ),
        do_not=(
            "Do not treat an unreadable file as an unchanged one and carry on as if the answers were current.",
        ),
    ),
    "installation_broken": ErrorRemedy(
        meaning=(
            "An upgrade stopped part way and this installation did not survive it. The check that produced this ran "
            "the same import the `agentic-hil` console script runs, through the same interpreter, after the package "
            "manager had stopped, and it failed: the package is gone. The console script itself usually is not, "
            "because it lives in a scripts directory rather than in the package's own, so `agentic-hil` still starts "
            "and dies with `ModuleNotFoundError: No module named 'agentic_hil'`. Nothing about the bench, the "
            "configuration or any board changed; what is missing is the software that talks to them."
        ),
        remediation=(
            "Run the line in `reinstall_command` on this result. It is the whole repair, and it is written for this "
            "machine: the interpreter that owns the installation and the extras `installed_extras` found before the "
            "upgrade started, because those were read while the metadata naming them still existed.",
            "Run it with the agent host closed. It reinstalls the same installation an MCP server would be running "
            "out of, and on Windows a file mapped as a running image cannot be replaced.",
            "Then run `agentic-hil --version` to confirm the console script answers again, and start the agent host, "
            "which loads the server from the repaired installation.",
            "If the reinstall reports that it cannot write where the old installation was, run it with the same "
            "scope the installation was created with, which for a per-user installation is `--user`.",
        ),
        do_not=(
            "Do not report this as an upgrade that failed and leave it there. The distinction this result draws is "
            "the whole of its content: an upgrade that fails normally leaves the previous release working, and this "
            "one did not.",
            "Do not retry the upgrade to get out of this. There is no installation left for an upgrade to move, and "
            "the manager will resolve against an environment that no longer has the package in it.",
            "Do not delete the scripts directory, the environment or the leftover console script to clean up first. "
            "The reinstall replaces what it needs to, and a hand-cleared PATH entry is one more thing to put back.",
        ),
    ),
    "upgrade_failed": ErrorRemedy(
        meaning=(
            "The package manager that owns this installation ran and did not finish, and the installation it was "
            "going to replace is still the one that was there. The second half is measured rather than assumed: once "
            "the manager had stopped, the same import the `agentic-hil` console script performs was run through the "
            "same interpreter, and it answered the version this process was already running. So nothing was replaced, "
            "nothing is half replaced, and the bench, the configuration and every board are exactly as they were; "
            "`previous_version` and `version` on this result are the same number, and `installation_intact` says so. "
            "Why it stopped is the manager's own account: `install` carries it when the manager produced any output, "
            "and `exception_type` with `detail` when the manager could not be run at all. The usual reasons are an "
            "index or a network that could not be reached, a TLS-intercepting proxy re-signing the connection to the "
            "index, a package manager that is broken or no longer where it was, and a release that was withdrawn "
            "between the resolution and the download."
        ),
        remediation=(
            "Read `install.stderr` on this result. That is the manager saying why it stopped, in its own words, and "
            "for most of these it is the whole diagnosis. The human rendering prints it as a literal block, so it is "
            "on the screen without `--json`; if there is no `install` at all, `exception_type` and `detail` say that "
            "the manager could not be started or did not return in time, which is a different thing from a manager "
            "that ran and refused.",
            "If this result carries `certificates`, the cause was a TLS-intercepting proxy and this command has "
            "already answered it once by itself: that clause says which store was tried and what came of it, and "
            "`next_steps` carries the one step measured for this machine. Read that clause and do what its `next_steps` "
            "says before anything else here, which is not always to install a CA: a retry against this machine's own "
            "store can get past the proxy and then fail for a reason of its own, and there the store already worked and "
            "the step is to keep it rather than to change it. A failure that carries no `certificates` met no proxy, so "
            "no trust store is the answer to it.",
            "Otherwise deal with the reason `install.stderr` gives and run the upgrade again. Nothing was removed, so "
            "there is nothing to undo first and the second attempt starts exactly where the first one did.",
            "If it keeps failing, the one-line installer is the repair path, and on a machine that already has an "
            "installation it repairs in place: `curl -LsSf https://agentic-hil.github.io/install.sh | sh`, or in "
            "PowerShell `irm https://agentic-hil.github.io/install.ps1 | iex`. It goes through the package manager "
            "again, recognises the same certificate signatures and retries against this machine's own store by "
            "itself, and re-registers the agent halves out of the fresh copy. Run it with the agent host closed.",
        ),
        do_not=(
            "Do not report this as a broken or a half-replaced installation. Those are two other answers, "
            "`installation_broken` and `installation_changed_after_failed_upgrade`, and this is the one where the "
            "probe found the previous release still loading. Telling an operator their bench is down when it is "
            "running sends them to a reinstall that nothing here needs.",
            "Do not reach for a switch that turns certificate verification off, on any manager. This project offers "
            "none, anywhere, and does not name one: a trust failure it could not answer is a CA to install, not a "
            "check to remove. TROUBLESHOOTING.md section 1 is the rest of it.",
            "Do not uninstall the package, delete the environment, or force a reinstall to give the next attempt a "
            "clean start. Nothing was removed by this failure, and the working installation this result names is the "
            "one such a cleanup destroys.",
            "Do not retry through `sudo pip` or `pip install --break-system-packages` because a system Python refused "
            "the install. That message is the distribution saying the interpreter is not yours to write into; `uv` "
            "and `pipx` install into environments of their own and need no exception.",
        ),
    ),
    "installation_changed_after_failed_upgrade": ErrorRemedy(
        meaning=(
            "An upgrade stopped part way, and it had already changed the files on disk before it stopped. The check "
            "that produced this ran the same import the `agentic-hil` console script runs, through the same "
            "interpreter, after the package manager had failed, and it loaded a version that is neither gone nor the "
            "one this process is running: the run replaced part of the installation and then exited non-zero. The "
            "server in memory is still the previous release named in `previous_version`; the disk is the other one in "
            "`version`. Because a failed run produced it, the on-disk version is not one to adopt by restarting onto "
            "it, even though it loads."
        ),
        remediation=(
            "Run the line in `reinstall_command` on this result. It restores a whole installation of a known version "
            "with the extras `installed_extras` recorded before the upgrade started, which is the way out of a "
            "half-changed tree rather than trusting whichever files the failed run happened to leave.",
            "Run it with the agent host closed. It replaces the installation an MCP server would be running out of, "
            "and on Windows a file mapped as a running image cannot be replaced.",
            "Then run `agentic-hil --version` to confirm which release answers, and start the agent host, which loads "
            "the server from the repaired installation.",
        ),
        do_not=(
            "Do not restart the agent host to pick up the version now on disk. It came from a run that reported "
            "failure, so the installation may be incomplete in ways a version number does not show, and a restart "
            "would put that half-changed tree into service.",
            "Do not report this as an upgrade that succeeded, or as one that failed and left the previous release "
            "working. Neither is true: the manager failed, and the previous release is no longer what is on disk.",
        ),
    ),
    "upgrade_blocked_by_pin": ErrorRemedy(
        meaning=(
            "The package manager holds this installation at one exact version, so the upgrade command it was given "
            "cannot move it and did not. `uv tool upgrade` reports that as success (exit code 0 with the reason on "
            "stderr) because for it, nothing to do is not a failure. The version this installation runs is unchanged; "
            "`previous_version` and `version` on this result are the same number, and that is what makes this a "
            "refusal rather than an upgrade. The pin comes from the requirement the installation was created with: "
            "`uv tool install \"agentic-hil==X.Y.Z\"` records `==X.Y.Z` and every later `uv tool upgrade` honours it."
        ),
        remediation=(
            "Close the agent host first. The command below reinstalls the environment, which on Windows means deleting "
            "it, and that delete fails while the MCP server the host started is still running out of it. `agentic-hil "
            "upgrade` moves the launcher on PATH out of the manager's way before it runs; a reinstall typed by hand "
            "has nothing of the kind, and the delete it starts with is the step that fails.",
            "Run the command in `reinstall_command` on this result. It is the one that clears the pin *and* keeps this "
            "installation's extras: `installed_extras` says which ones were found, and reinstalling without them "
            "removes what they installed. On a bench with `can`, that silently takes CAN support away. The hint `uv` "
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
            "from any directory. It preserves the extras the installation was created with, and it is not refused "
            "because this server is running: it names this server under `restart_required_by` and the host restart "
            "is what adopts the new release.",
            "`agentic-hil --version` beside this result's `running_version` is how an operator checks whether a newer "
            "release is even the difference they are chasing.",
        ),
        do_not=(
            "Do not run `uv tool upgrade`, `pipx upgrade` or `pip install --upgrade` through a shell instead. This is "
            "the same action the configuration just refused, taken where the operator cannot see or audit it, and "
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
            "govern: the same objection as changing a permission mid-run. Nothing was replaced and the holder was "
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
            "image, and this server is running out of the very installation an upgrade would replace. A package "
            "manager that removes the environment before rebuilding it fails on that delete part way through and "
            "leaves neither the old installation nor the new one, so nothing was attempted. The alternative, a helper "
            "that swaps the files after this server exits, was rejected: it outlives the result that announced it, so "
            "a failure would have nobody to report to and would produce exactly the half-replaced environment this "
            "refusal exists to prevent."
        ),
        remediation=(
            "Ask the operator to run `agentic-hil upgrade` at a shell. It runs as a separate process, so the "
            "environment it replaces is not the one it is running out of; it upgrades through the manager that owns "
            "the installation and keeps the extras it was created with; and it moves the launcher on PATH aside first, "
            "so the copy that used to fail against a mapped image lands.",
            "That command is not refused because this server is running. It names this server under "
            "`restart_required_by`, which says what is left to do: restart the host, which is what loads the new "
            "release. Report `running_version` from this result as the version still in force until that has happened.",
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
            _CONFIG_MISSING_MCP_ROUTE,
            _CONFIG_MISSING_SHELL_ROUTE,
            _CONFIG_MISSING_WHAT_IT_WRITES,
            _CONFIG_MISSING_REPORT_AND_ASK,
        ),
        # The same four, for the person who typed `agentic-hil doctor` and met
        # this before any configuration existed. They were being told to call an
        # MCP tool and then to "report what it granted and ask the operator",
        # which is the agent's job description read out to the operator; the
        # command they can run stood second and spoke about them in the third
        # person.
        cli_remediation=(
            _CONFIG_MISSING_SHELL_ROUTE,
            _CONFIG_MISSING_MCP_ROUTE,
            _CONFIG_MISSING_WHAT_IT_WRITES,
            _CONFIG_MISSING_REPORT_AND_ASK,
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
            "A field-wise configuration change reached a description key (what the bench is), and "
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
            "`false` into a permission, never `true`, whatever `allow_config_permissions_write` says. A generated "
            "configuration grants that key, so it is very likely open here and still not a way back to this one.",
        ),
    ),
    "permission_denied:allow_config_permissions_write": ErrorRemedy(
        meaning=(
            "A field-wise configuration change reached a permission (what the bench may be told to do), and "
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
            "Do not route the change through another key to reach a permission: a whole subtree, a differently "
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
            "Relay `operator_command` to the operator (it is the whole line, with this incident's id already in it) "
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
    "permission_denied:allow_debug_execution": ErrorRemedy(
        meaning=(
            "`debug_continue`, or any other call that would resume a halted target, was refused because "
            "`permissions.allow_debug_execution` is false on this probe. A debug session may still open and the "
            "target may still be inspected while it sits halted: breakpoints, symbol reads, memory dumps and "
            "`debug_halt` all read or hold the target rather than resume it, and none of them need this grant. Only "
            "letting the core run again does. Nothing was sent to the target and the session, if one is open, is "
            "unchanged."
        ),
        remediation=(
            "Report the refusal, name `permissions.allow_debug_execution`, and say which probe it is on. Whether "
            "this bench should let an agent resume the target is the operator's decision; you cannot grant it "
            "yourself, and `project_config_set` writes only `false` into a permission.",
            "Everything a session can still do with the target halted remains available: set or clear breakpoints, "
            "read `debug_get_stop_reason`, `debug_symbol_info`, `debug_symbol_value` or `debug_dump_symbol_ihex`, and "
            "close the session with `debug_stop_session`. Finish the part of the task that only needs a halted "
            "target before asking about the rest.",
            "`project_config_describe` says whether this permission is open on the bound probe right now, so a "
            "second attempt at `debug_continue` is not how to find out.",
        ),
        do_not=(
            "Do not reach for `reset_target` or `flash_firmware` as a way around this. Both are gated by their own "
            "permissions and neither resumes the target under the debugger session this refusal is protecting.",
            "Do not drive GDB, OpenOCD or another debugger outside Agentic HIL to send the continue yourself. That "
            "reaches the exact target state this refusal withholds, outside the audit trail that would have recorded "
            "it.",
        ),
    ),
    RECOVERY_PHYSICAL_CHECK_ERROR: ErrorRemedy(
        meaning=(
            "`hardware_recover` was allowed to run and refused on the class of the incident, not on a permission. At "
            "least one reason this bench is quarantined for names a physical state (a flash whose outcome was never "
            "confirmed, a session that died mid-call, cleanup nobody could verify), and clearing it means saying that "
            "the board is still and holds the firmware somebody expects. That is a claim about the world, and only a "
            "person can make it. You cannot make it; you can ask for it and carry it. Pass what the operator answers "
            "as `operator_statement` and it goes into the recovery ledger as their words, relayed by you. "
            "`physical_check_reasons` names the reasons that need one, `agent_clearable_reasons` the ones that clear "
            "with no arguments at all. No grant on any bench moves that line, which is why the argument is a sentence "
            "and not a flag: a flag you could set for yourself, and a confirmation one gives oneself is not one. A "
            "reason this bench's `recovery.auto_recover` policy could settle needs neither: the automatic path runs "
            "its predicate against the board on the next hardware call. `auto_recoverable` says which case this is."
        ),
        remediation=(
            "Read `auto_recoverable` first. When it is true this bench's own recovery policy can settle the incident "
            "by running a predicate against the board (a re-read of the probe, or a verified reset into halt), and it "
            "does that on the next hardware call, not here: this tool runs no predicate, so it will not assert an "
            "unconfirmed board is fine. Retry the hardware call once and read the result.",
            "Otherwise ask the operator, in chat, and show them `quarantine_guidance` while you ask: what was "
            "attempted, what is still confirmed, what nobody on this host can know, and the `physical_check` to "
            "perform on the board. A statement about a claim the speaker was never shown is worth nothing.",
            "Call `hardware_recover` again with `operator_statement` set to what they answered, in their words. Do "
            "not compress it into a verdict: the ledger keeps the sentence, and a sentence can be audited in a way "
            "that 'the operator confirmed' cannot.",
            "When there is nobody to ask, relay `operator_command` verbatim instead. It is the exact command, with "
            "this incident's `quarantine_id` already in it, and it is what the operator runs at the bench. Say "
            "plainly that hardware effects stay blocked until then, and stop there.",
        ),
        do_not=(
            "Never invent an `operator_statement`. A line that reflects no actual operator utterance is a false "
            "ledger record with you named in it as the actor who cleared the bench, and the next person to read that "
            "ledger has no way to tell it from one somebody really said.",
            "Do not strengthen what you were told on the way through. 'It should be fine' is not 'the board is "
            "powered down and still', and the ledger has to carry the difference.",
            "Do not retry the hardware call to 'clear' the incident. The bench's own recovery policy already tried "
            "whatever it could verify without an operator, before this refusal existed.",
            "Do not clear the state files under `state_root` by hand, and do not ask the operator to. The routes "
            "above are the supported ones and they keep the ledger line saying who cleared what, and on what.",
        ),
    ),
    CONFIG_WIDENING_ERROR: ErrorRemedy(
        meaning=(
            "A field-wise configuration change tried to write something other than a narrowing into a permission. A "
            "generated configuration starts with every permission granted but the two that refuse flashing while they "
            "are true, and the one direction `project_config_set` "
            "leaves is narrowing, so an agent writes `false` into a permission and never `true`. Two cases share this "
            "error because for a caller they are one fact: nothing here turns a permission on. Either the request "
            "carried a permission value that was not `false`, and `widened_keys` names those keys as the request spelled "
            "them, including a `true` sent to a permission that is already `true` and would have moved nothing; or the "
            "resulting document would have granted more than the current one, and `widened_keys` names the paths that "
            "would have opened, read out of the document before and after rather than out of the request. Nothing was "
            "written in either case, including the parts of the same call that were narrowings: a change is applied "
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
            "`agentic-hil doctor` names the entry, says of each value whether it is an OpenOCD search name or a path, "
            "and for a path whether the file is there.",
            "Set them with `project_config_set`, either to OpenOCD's own script names for this probe and target "
            "(`interface/stlink.cfg`, `target/stm32f4x.cfg`), which the installed OpenOCD resolves against its script "
            "path, or to absolute paths of the script files. A configured script path must be absolute and must live "
            "outside the workspace.",
            "If the search names do not resolve, the OpenOCD on this machine has no script tree where it expects one: "
            "install the scripts, or point `OPENOCD_SCRIPTS` at them, or name the files by absolute path.",
        ),
        do_not=(
            "Do not copy OpenOCD scripts into the repository and point the configuration at them. A script inside the "
            "workspace is repository-controlled Tcl running in the debugger, and a configured absolute path inside the "
            "workspace is refused at load for that reason.",
            "Do not write a script under the system temporary directory and point the configuration there. It is "
            "cleared without warning, so the file would describe this bench only until the next reboot, and the "
            "configuration refuses such a path at load.",
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
            "`agentic-hil lease-status` names the holder (which devices are held, which frontend took them and under "
            "which process), and `open_holds` in this refusal carries the same.",
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
            "description on disk decides which physical unit each of those names means, so re-reading it mid-run could "
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
            "where a directory was needed, or the final object is not a single-link regular file, or its parent names "
            "one place and resolves to another. The path was refused "
            "before anything was read from it or written to it, because following it would act on an object other than "
            "the one the configuration names. "
            "The last of those has no symlink in it at all and is what a packaged agent host does to this profile: an "
            "MSIX AppContainer virtualizes %APPDATA% and %LOCALAPPDATA%, so a path under either creates, opens and "
            "writes exactly as it reads while resolving onto the package's private LocalCache tree. Walking such a "
            "chain finds nothing: no reparse point, no symlink, link count 1, and samestat holds, because the "
            "indirection lives in name resolution alone. "
            "This is about what the path *is*, not about who else on the machine holds rights on it. That second "
            "question used to be asked (a Windows ACL walk and a POSIX mode/sticky-bit walk over every ancestor), and "
            "it was removed in 0.8.0: it could only ever defend against a different account on the same "
            "machine, which was never a requirement here, and it could never defend against the operator's own "
            "processes, which own these objects and can rewrite them regardless of any ACL."
        ),
        remediation=(
            "Read `resolved_parent` first when the refusal carries one: the parent of `path` resolves to that other "
            "spelling, and the resolved spelling is the one that works. Point the setting at it, or at a location "
            "outside the redirected tree. Do not go looking for a symlink; on this profile there is none to find.",
            "Read `component` when the refusal carries one: that is the part of the chain that stopped the walk, and "
            "there the object really is a symlink or a file where a directory was needed. Replace it with a real "
            "directory, or point the setting at a path that does not go through it.",
            "{safe_user_root} is a location this tool creates for itself and is a safe answer when the discovered "
            "default cannot be used. `agentic-hil init` and `project_config_create` fall back to it on their own for "
            "both the configuration and the state_root, so re-running either is usually the whole fix.",
            "This refusal is about a path, so it is deterministic: the same command with nothing changed is refused "
            "again, with a new run id and the same two spellings. Repair the setting instead. Which setting is read "
            "off `path`, against the two roots `agentic-hil doctor` prints as `config_path` and under `State root`: a "
            "`path` under the configured `state_root` (every report, log, lease and audit record is written there) "
            "means the configuration names a root this profile will not accept, and `agentic-hil init --force` (or "
            "`project_config_create`) rewrites the file with one it will. Any other `path` is one an operator set, and "
            "it is changed in the file before the command is run again.",
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
            "opened and nothing was written to it. A device name (`COM7`, `/dev/ttyACM0`) is an enumeration order "
            "rather than an identity, so attaching a second adapter or replugging in another order can hand one entry "
            "another board; this refusal is that having happened. `expected_vid`/`expected_pid` and "
            "`found_vid`/`found_pid` are the same comparison one level up, and they are present when the entry names "
            "them: a USB serial number is unique only within a vendor, so a serial that matches under a foreign vendor "
            "or product id is refused too, and an entry for an adapter that publishes no serial at all is compared on "
            "these alone."
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
            "Do not change `serial_number`, `vid` or `pid` to the values that were found, and do not delete those keys. "
            "That turns the one check that noticed into agreement with whatever is plugged in, which is the silent "
            "wrong-board flash this refusal exists to prevent. Change the board or change `device`; the identity is not "
            "the thing to edit.",
        ),
    ),
    COM_PORT_BUSY_ERROR: ErrorRemedy(
        meaning=(
            "The serial device named by `com_ports.<name>.device` is already held by another program, so the session "
            "was refused where the operating system refused the open: no handle was created, nothing was written, and "
            "the port kept doing whatever the other holder is doing with it. `configured_device` is the device name "
            "that was tried. This is a refusal about which process owns the port right now, not a quarantine: the "
            "bench was not touched and no incident was opened.\n\n"
            "The session asks for the port exclusively rather than sharing it. Sharing was never a mode this could "
            "work in: two writers on one line interleave their bytes, and the failure then surfaces much later as a "
            "response that does not match the stimulus, which is an unknown board state, and an unknown board state "
            "is a quarantine. Failing at the open turns that into this refusal."
        ),
        remediation=(
            "Find the holder and stop it. On Linux, `fuser -v <device>` or `lsof <device>` names the process; on "
            "Windows, close the terminal, IDE serial monitor or flashing tool that has the port open.",
            "A second test runner, an open serial monitor and a modem manager are the three usual holders. On Linux, "
            "`ModemManager` probes new serial adapters on its own and can hold one for several seconds after it is "
            "plugged in; retrying shortly afterwards is enough, or exclude the adapter from it by udev rule.",
            "Retry the session once the port is free. The bench was never blocked: `retry_safe` is true and there is "
            "nothing to recover.",
            "If the holder is a second Agentic HIL run on this host, let it finish. Two runs on one board is what the "
            "device lock exists to prevent, and it reports that case as `device_busy` with the holder named.",
        ),
        do_not=(
            "Do not run `recover --confirm-safe-state` over this. No lease was quarantined and no board state is in "
            "question; signing for a physical state nobody disturbed teaches the signature to mean nothing.",
            "Do not work around it by pointing the entry at a different device name. The other name is a different "
            "board, and a stimulus sent to the wrong board is the failure the port identity check exists to prevent.",
        ),
    ),
    "com_port_not_bound": ErrorRemedy(
        meaning=(
            "A configured `com_ports` entry names a port and no device, so there is nothing to open. `field` names the "
            "key that is empty. The entry exists because the project declared this port (its name, its baudrate and "
            "its permissions) before any bench was attached: `agentic-hil init` writes the project profile's ports "
            "that way when discovery found no board. Nothing was contacted and nothing is in doubt.\n\n"
            "It is deliberately not `com_port_not_configured`, which means the project declares no such port at all. "
            "There the caller or the plan named a port that does not exist; here both are right and the bench is not "
            "filled in, and telling an operator their plan referenced an unconfigured port sent them to look for a "
            "mistake that was not there."
        ),
        remediation=(
            "Plug the board in and run `agentic-hil adopt-hardware`. It fills `com_ports.<name>.device` in "
            "from the attached hardware, together with the serial number and USB ids that make the name checkable, "
            "and it fills in the probe and the toolchain path in the same call.",
            "If the port is not the probe's own virtual COM port, run `agentic-hil com-ports` to see what this host "
            "has and name the device yourself. On Linux prefer the `/dev/serial/by-id/...` name, which survives a "
            "replug.",
            "Nothing needs recovering: `retry_safe` is true, no handle was created and no line was driven.",
        ),
        do_not=(
            "Do not delete the entry to make the refusal go away. The entry is the project's statement that this bench "
            "has a `dut_uart` at this baudrate with these permissions, and a plan that names it is correct; removing "
            "it turns a precise refusal back into `com_port_not_configured`.",
            "Do not point it at whichever device happens to be free. A serial device name is an enumeration order, so "
            "a guess is a stimulus sent to whatever board took that name.",
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
    # The refusal every test plan that does not run lands on, and for a long
    # time the one that said only what was wrong. A plan is the first thing a
    # newcomer writes that this project executes, so a reader who reaches one is
    # further in than a reader who reaches `config_file_not_found` and has more
    # to lose by guessing: the three answers below are the whole of what the
    # refusal can mean, and which one applies is in the result's own
    # `validation_error`.
    "test_config_invalid": ErrorRemedy(
        meaning=(
            "A test plan was refused before its first step ran, so nothing was locked, opened or driven. The plan is "
            "one document and the authoritative configuration is another, and this refusal is the two disagreeing: "
            "either the plan is not valid against the plan schema, or it is valid and names a device this bench does "
            "not have. `validation_error` says which. `field` there is the dotted path into the plan "
            "(`steps[2].port_id`), `step` the step number the report repeats as `failed_step`, and `next_step` the "
            "one move that fixes this case. A refusal about a name also carries what the configuration does declare, "
            "under `configured_com_ports`, `configured_can_buses` or `configured_debuggers`."
        ),
        remediation=(
            "Read `next_step` inside `validation_error` first. It names the key that is wrong and the command or the "
            "configuration field that fixes it; the steps below are the general form of the same three answers.",
            "A step naming a device the configuration does not declare is corrected in the plan: `device:` has to be "
            "one of the names the refusal lists under the `configured_*` key for that kind.",
            "A bench that genuinely has no such entry is filled in rather than argued with. With the board attached, "
            "`agentic-hil adopt-hardware` fills a declared debugger or COM-port entry's hardware in; adoption "
            "has no CAN half, so a `can_buses` entry's adapter and channel are written by hand. Where the section is "
            "empty because `agentic-hil init` ran with no bench attached, `agentic-hil init --force` from the project "
            "root writes the file again from the project profile and the hardware it finds. `--force` replaces the "
            "whole file, every narrowed permission included, so it is a reset rather than a repair.",
            "A `field` naming a plan key rather than a device is a plan the schema rejects. "
            "`{test_plan_reference}` is what it was validated against; it reads the shipped schema rather than "
            "describing it, so the version that admits a step is in the same table as the step.",
            "Nothing needs recovering. The whole plan is refused at preflight, before the first hardware action, so "
            "no session was opened and no board was driven.",
        ),
        do_not=(
            "Do not add a `com_ports`, `can_buses` or `debuggers` entry just to make a plan load. A plan naming a "
            "device this bench does not have is a plan for another bench, and an entry written to satisfy it is a "
            "stimulus pointed at whatever hardware took that name.",
            "Do not raise the plan's `version:` to reach a step it refuses. The version is what the plan was written "
            "against, and a step that is too new for it is refused by name here rather than failing on an older "
            "install for no stated reason.",
        ),
    ),
    "run_already_active": ErrorRemedy(
        meaning="This owner already holds an open run, and a run declares its devices once, up front.",
        remediation=(
            "End the open run before declaring another; the devices it declared are released then.",
            "One run per owner is what makes the declared set the complete answer to what this owner may touch.",
        ),
    ),
    # The most common refusal on this surface, and for a long time the one that
    # carried nothing: every other entry here explains a bench, a policy or a
    # backend, and this one explains the caller's own payload. It is deliberately
    # general (the concrete fact is always in the result's own `field`) because
    # a per-argument entry would be a second copy of the input schemas that
    # nothing keeps in step with them.
    "invalid_argument": ErrorRemedy(
        meaning=(
            "The call was refused on its arguments alone, before anything was locked, opened, or driven. Nothing was "
            "reached and there is nothing to clean up. What was wrong is in the result rather than here: `field` names "
            "the argument (dotted for a nested one, `$` for the object itself), and `validator` names the rule it "
            "broke where a schema decided it (`type`, `minimum`, `maximum`, `required`, `enum`, "
            "`additionalProperties`, `finite`). `allowed_values` appears when the rule was an enumeration, and `value` "
            "when the refusal came from a configuration or profile document rather than from a tool argument. This "
            "says the request as written is not answerable; it says nothing about whether the device, the probe or the "
            "bus is available."
        ),
        remediation=(
            "Read `field` and `validator` together and repeat the call with that one argument corrected. Validation "
            "stops at the first fault, so a second wrong field is named on the next attempt rather than now.",
            "Take the accepted shape from the tool's own `inputSchema` in `tools/list`, not from a remembered example. "
            "That schema is what the refusal was decided against.",
            "Types are checked as written and never coerced, so a value that means something other than what it would "
            "convert to cannot pass as the converted one: `wait_s: true` and `wait_s: \"5\"` are both refused where "
            "`wait_s: 5` is taken.",
            "When `field` names a configuration or profile key (`com_ports.<name>.baudrate` and the like), the fix is "
            "in the document the refusal names and not in the call. `agentic-hil://reference/config-shape` gives the "
            "expected shape of each key; correct it there, then repeat the call.",
        ),
        do_not=(
            "Do not retry the identical payload. Nothing here is timing or contention, and the same arguments are "
            "refused the same way every time.",
            "Do not drop a refused optional argument and let its default stand unless the default is what was meant. A "
            "wait that is refused and then omitted becomes no wait at all, and the call fails on `device_busy` "
            "instead: the same request failing one layer later for a reason that is not the real one.",
            "Do not read this as a hardware or a permission problem. Nothing was contacted, so there is no state to "
            "recover and no permission to ask the operator for.",
        ),
    ),
    # The only entry here that is about this server's own output rather than
    # about a bench, a policy or a payload. It is what a reader gets in place of
    # a result the rendering could not vouch for, so it has to say that the
    # command itself is not what went wrong.
    REDACTION_UNAVAILABLE_ERROR: ErrorRemedy(
        meaning=(
            "Nothing on the bench failed and nothing was refused. The command produced its result, and the step that "
            "replaces secret-named values before a result leaves this process did not hand back a document. Both "
            "sinks depend on that step -- the prose a person reads and the `--json` document alike -- so neither can "
            "publish this result, and this was emitted in place of it by whichever one you asked for. The command "
            "exits nonzero because a result nothing could vouch for is a result that was not delivered."
        ),
        remediation=(
            "Report it, with the command that produced it and `agentic-hil --version`. Redaction answers a document "
            "with a document for every document it is given, so no result's own contents can reach this: what it "
            "names is an installation whose `agentic_hil.redact` is not the one this server ships. `--json` is not a "
            "way around it, because that sink redacts through the same function and fails closed the same way.",
            "Check for a second copy of the package while collecting that: `python -c \"import agentic_hil; "
            "print(agentic_hil.__file__)\"` names the one actually imported, and a user-site install shadowing the "
            "intended one is the way two versions end up in a single process.",
        ),
        do_not=(
            "Do not read this as a hardware, permission or configuration failure. It says nothing about what the "
            "command did or left behind; it says only that the result was not rendered.",
            "Do not reach for another way to print the result, `--json` included. The one thing known about it here "
            "is that nothing has vouched for its secret-named values, which is what both sinks declined to publish.",
        ),
    ),
    "target_not_detected:openocd": ErrorRemedy(
        meaning="OpenOCD reached the debug adapter but no target answered on the selected transport.",
        remediation=(
            "Confirm the probe enumerates at all: call debugger_probes_list. On OpenOCD it reads this host's USB "
            "serial inventory, which sees an ST-Link only through the virtual COM port it publishes; a result carrying "
            "`complete: false` found no such ST-Link, but a standalone ST-LINK/V2 with no VCP would not appear there, "
            "so read the id off the probe before concluding it is missing.",
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
    # without confirming what it did places it nowhere (whether it printed part of
    # the confirmation or none of it), so the same public error_type would have
    # published exactly the claim this branch cannot make.
    "target_state_unconfirmed:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD exited successfully without the tool's own success marker, so its account of this run is "
            "incomplete rather than negative. Which part is missing is in this result and not in this entry: "
            "`operation_result.expected_success_text` lists both markers the backend `echo`es (the stage marker after "
            "`init` and the success marker at the end of the command), and `matched_success_text` names the ones "
            "OpenOCD printed, which may be neither of them or the stage marker alone. Either way the outcome went "
            "unreported, and that is the absence of a verdict rather than a verdict that no target answered: `init` "
            "may have completed, examined the core and halted it. The board's run state is unknown, which is why this "
            "quarantines the bench instead of refusing.\n\n"
            "The success marker is what this branch turns on, and it settles the opposite case too: a run that exits 0 "
            "*with* the marker is a success even when OpenOCD printed a failure-worded line on the way to it, because "
            "OpenOCD stops evaluating its command string at the first command that fails and could not have reached the "
            "`echo` otherwise. Those lines arrive verbatim on the successful result as `backend_warnings`, with the "
            "summary saying how many came along, rather than deciding an outcome the marker already reported."
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
            "this backend rather than a fault on the bench. Report it with that log.",
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
            "STM32CubeProgrammer exited successfully without every line that confirms the operation (for a read, the "
            "ST-Link serial number and the device name), so its account of this run is incomplete rather than "
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
    "probe_inventory_incomplete": ErrorRemedy(
        meaning=(
            "Bootstrap discovery would not bind a probe off a count it cannot prove complete. STM32CubeProgrammer is "
            "not installed, so probes are enumerated from this host's USB serial inventory, which reaches an ST-Link "
            "only through the virtual COM port a V2-1 or a V3 publishes. A standalone ST-LINK/V2, or any probe that "
            "exposes no VCP, can be attached and never appear, so an empty reading is not proof no probe is connected "
            "and a sole visible one is not proof it is the only one. Selecting a board off that reading would be a "
            "silent choice a later flash or reset trusts, and the caveat rides the discovery answer rather than the "
            "configuration the choice is written into, so `project_config_create` refuses and `agentic-hil init` writes "
            "an unbound placeholder instead of binding one."
        ),
        remediation=(
            "Name the intended board's serial as probe_id: `agentic-hil adopt-hardware --probe-id <serial>` binds it, "
            "and the serials this host can see are under `probes` (and from `agentic-hil debugger-probes`). Over MCP on "
            "a workspace with no configuration yet, ask the operator to run `agentic-hil init` first, which writes the "
            "unbound placeholder that adoption then fills, because `agentic-hil adopt-hardware` loads an existing "
            "configuration and there is none to load.",
            "Or install STM32CubeProgrammer for an authoritative count: its own listing reads the serial off the probe "
            "directly rather than through a virtual COM port, so it sees a VCP-less ST-LINK/V2 the inventory cannot, "
            "then run the generation again.",
        ),
        do_not=(
            "Do not read this as `adapter_not_found` or an absent bench. An empty inventory here is a blind spot, not a "
            "proof that no probe is attached: a VCP-less ST-LINK/V2 could be plugged in right now, so reseating or "
            "re-attaching hardware that is already there is the wrong move.",
            "Do not bind the sole visible serial as though the count were complete. A VCP-less probe beside the one the "
            "inventory saw would be the board a later flash or reset silently reaches, which is exactly the choice this "
            "refusal exists to keep an operator, not the tool, from making.",
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
    "flash_erase_failed:stlink": ErrorRemedy(
        meaning=(
            "The device refused to erase the flash STM32CubeProgrammer was about to write, and the programmer said so "
            "in its own words: `Error: failed to erase memory`. The whole transcript travels with the result under "
            "`programmer_output`, because the line that names the failed operation is the diagnosis. Nothing was "
            "verified.\n\n"
            "How far the failure can be placed is answered by `erase_abort_point`, read off that same transcript, and "
            "it is a diagnostic reading rather than a safety verdict: a refused erase leaves the flash contents "
            "unconfirmed, so the `debugger_result_unconfirmed` quarantine stands whichever reading fits. "
            "`erase_refused_effect_unconfirmed` means the refusal is there and no line reports a write completing, but "
            "the transcript does not establish that no sector was erased before the refusal, its progress and download "
            "lines are not guaranteed across versions, quiet or piped output and abort paths, and `failed to erase "
            "memory` also covers a protection refusal that can strike after unprotected sectors have already gone. "
            "`flash_change_underway` means a line says a phase had started, so flash is neither the old image nor the "
            "new one. `abort_point_unreadable` means the transcript does not place the failure at all. `evidence_line` "
            "is the line each reading was taken from.\n\n"
            "The measured case on a NUCLEO-F446RE is the first flash after power-up, refused after about 310 ms with "
            "the device correctly identified, while every immediate retry programmed and verified. That pattern is a "
            "core still executing from flash while the programmer connects in hot-plug mode, not a wiring fault: this "
            "used to be reported as `reset_failed` with `reset line wiring issue` among its causes, and the reset line "
            "was never the thing that was wrong."
        ),
        remediation=(
            "Retry the flash once. On the bench this was measured on, a core still executing from flash under hot "
            "plug defeated the erase and every immediate retry programmed and verified, so the retry is the "
            "substantive fix. Nothing has to be recovered first: this is an ordinary `debugger_result_unconfirmed` "
            "incident that owes no gate. When the failed call is a bare `flash_firmware`, its own implicit "
            "single-action run, that incident stands down the moment the call ends: the result carries "
            "`incident_stood_down` and `quarantined: false`, the bench is handed back automatically, and the next "
            "flash is simply accepted. When the call ran inside a declared run (`bench_run_start` … "
            "`bench_run_stop`), the run owns the hold, so the failed result stays `quarantined: true` with no "
            "`incident_stood_down`, and the declared run keeps the probe until `bench_run_stop` performs the run "
            "teardown and the same stand-down then. Either way the incident owes nobody a signature: "
            "`hardware_recover` over it answers `nothing_to_recover: true`, because there is nothing standing to "
            "clear, and a retry is accepted without a recovery step rather than being refused with "
            "`resource_quarantined`. `recover --confirm-safe-state` is for a quarantine that actually holds the "
            "bench, such as `resource_quarantined` or a broken audit, which this is not. It is not a free retry, "
            "though: a refused erase does not prove the flash is untouched, which the result still says, "
            "`cleanup_required` stays true and `cleanup_reasons` still names `debugger_result_unconfirmed`, so the "
            "board holds an indeterminate image until a retry programs and verifies.",
            "Read `programmer_output.stdout` before anything else. It is the programmer's own account of what it "
            "erased, wrote and verified, and it is what says which operation stopped.",
            "Treat the board as holding an indeterminate image whichever reading `erase_abort_point` gave, "
            "`erase_refused_effect_unconfirmed` no less than `flash_change_underway` or `abort_point_unreadable`, "
            "because none of them proves the flash is unchanged. Reflashing is the way through it, not a retry taken "
            "as proof the erase never happened; the reflash needs no recovery step ahead of it, a bare call's "
            "incident has already stood down, and a declared run's is one the reflash is allowed to run under, but "
            "read it as writing over an unknown image rather than a clean one.",
            "If the refusal repeats on the retry, ask the device about protection rather than about wiring: read the "
            "option bytes with STM32CubeProgrammer yourself (`-ob displ`) and look for read-out protection, write "
            "protection or PCROP over the sectors the image covers.",
            "The durable fix for a core that defeats the erase is connecting under reset for the flash, so the core is "
            "held in reset while the probe attaches and never runs during the erase. Set "
            "`debuggers.<name>.connect_mode` to `under_reset` (STM32CubeProgrammer's `mode=UR`); `project_config_set` "
            "writes it, because it sits under `allow_config_description_write` and grants nothing the flash did not "
            "already allow. Only `type: stlink` carries it and only `flash_firmware` reads it, so `probe_target`, "
            "`reset_target` and the memory reads keep the connect their own operation decides. It needs the probe's "
            "reset line wired to the target's NRST, which an on-board ST-Link already has and a board wired with "
            "SWDIO, SWCLK and ground alone does not, there the connect fails rather than falls back. A running MCP "
            "server puts the change in force with `project_config_reload_description` or a restart, because "
            "`connect_mode` is a description key it re-reads and not a permission. Until then the retry above is the "
            "workaround, and the plan is the place for that rather than somebody's memory.",
        ),
        do_not=(
            "Do not read this as a reset problem. No reset failed, and re-seating the reset line, changing "
            "`debuggers.<name>.interface` or power-cycling on that theory changes nothing about a refused erase.",
            "Do not grant `allow_mass_erase` to force the erase through. That permission makes this service refuse "
            "flashing outright, it erases the whole device rather than the sectors the image covers, and it answers a "
            "protection refusal by destroying more than the failed operation ever asked for.",
        ),
    ),
    "flash_erase_failed:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD could not erase the flash sectors the image covers, and said so in its own words: `failed erasing "
            "sectors <first> to <last>`. Nothing was written and nothing was verified, and the flash contents are "
            "unconfirmed rather than known-unchanged: the sectors named before the failing one may already be erased.\n\n"
            "This used to be reported as a plain `flash_failed`, whose causes are about a wrong image or a wrong "
            "address and say nothing about an erase. Worse, whenever the transcript carried an unrelated reset line, "
            "and OpenOCD warns on nearly every `reset halt` that it is only resetting the core, the classification "
            "became `reset_failed` and sent the operator to the reset line instead. The rule now reads the line "
            "OpenOCD wrote about the operation that stopped."
        ),
        remediation=(
            "Read `programmer_output.stdout` and `programmer_output.stderr` on the result before anything else. They "
            "are OpenOCD's own account of what it opened, examined and tried to erase, and `failed erasing sectors "
            "<first> to <last>` in them names the sector range, which is what says whether the refusal covers the "
            "whole image or starts partway into it. The log the result names by `log_path` holds the same capture.",
            "Ask the device about protection rather than about wiring. Read the option bytes for read-out protection, "
            "write protection or PCROP over the sectors the range names, with a vendor tool or with OpenOCD's own "
            "`flash info <bank>`, and clear the protection deliberately if that is what it shows.",
            "Check that the flash bank OpenOCD erases by is this device's. It comes from "
            "`debuggers.<name>.target_cfg`, and a configuration written for a near neighbour of this part declares "
            "sector sizes the device refuses at the erase while the connect and the identification both succeeded.",
            "Treat the board as holding an indeterminate image until a flash programs and verifies. A refused erase "
            "does not prove the flash is unchanged, which is why the result says the contents are unconfirmed, and "
            "reflashing is the way through it rather than a retry taken as proof the erase never happened.",
        ),
        do_not=(
            "Do not read this as a reset problem. No reset failed, and re-seating the reset line or changing "
            "`debuggers.<name>.interface_cfg` on that theory changes nothing about a refused erase.",
            "Do not reach for a device unlock command such as `stm32f2x unlock` to force the erase through. Those "
            "answer a protection refusal with a mass erase of the whole part, which destroys more than the failed "
            "operation ever asked for; the same reasoning is why this service refuses to flash at all once "
            "`allow_mass_erase` is granted.",
        ),
    ),
    "flash_erase_failed:pyocd": ErrorRemedy(
        meaning=(
            "pyOCD could not erase a flash sector the image covers, and said so in its own words: `Failed to erase "
            "sector at <address>`. Nothing was written and nothing was verified, and the flash contents are unconfirmed "
            "rather than known-unchanged: the sectors before the failing address may already be erased.\n\n"
            "This used to be reported as a plain `flash_failed`, and pyOCD logs `Resetting target` as a matter of "
            "course beside what it is doing, so the same failure with that line in the transcript came back as "
            "`reset_failed`: the failure of an operation that had in fact succeeded. The rule now reads the line "
            "pyOCD wrote about the operation that stopped."
        ),
        remediation=(
            "Read `programmer_output.stdout` and `programmer_output.stderr` on the result before anything else. They "
            "are pyOCD's own account of what it loaded and tried to erase, and `Failed to erase sector at <address>` "
            "in them names the address the device refused, which is what places the failure inside the image. The log "
            "the result names by `log_path` holds the same capture.",
            "Ask the device about protection rather than about wiring. Read the option bytes for read-out protection, "
            "write protection or PCROP over the sector that address falls in, with the vendor's own tool, and clear the "
            "protection deliberately if that is what it shows.",
            "Check that the sector map pyOCD erases by is this device's. It comes from the CMSIS pack behind "
            "`debuggers.<name>.target_type`, so a target type that resolves to a near neighbour of this part erases at "
            "addresses the device refuses while the connect and the identification both succeeded. "
            "`agentic-hil doctor` reports what the configured value resolves to, in "
            "`debuggers.<name>.target_support`.",
            "Treat the board as holding an indeterminate image until a flash programs and verifies. A refused erase "
            "does not prove the flash is unchanged, which is why the result says the contents are unconfirmed, and "
            "reflashing is the way through it rather than a retry taken as proof the erase never happened.",
        ),
        do_not=(
            "Do not read this as a reset problem. No reset failed, and re-seating the reset line or power-cycling on "
            "that theory changes nothing about a refused erase.",
            "Do not answer it with a chip erase (`pyocd erase --chip` or a `--erase chip` flash). That erases the whole "
            "device rather than the sectors the image covers, and it answers a protection refusal by destroying more "
            "than the failed operation ever asked for; the same reasoning is why this service refuses to flash at all "
            "once `allow_mass_erase` is granted.",
        ),
    ),
    # -- A capability this configuration does not have, and the way to one ------
    # Scoped per backend, because "not supported" is only half an answer: the
    # useful half is which configuration would support it, and that differs by
    # what is plugged in. Both entries end at the same place (the probe on the
    # bench is one OpenOCD drives), and both say what the move costs, because a
    # way out whose price is discovered afterwards is a dead end with a delay.
    "not_supported:stlink": ErrorRemedy(
        meaning=(
            "This bench runs `type: stlink`, which drives STM32CubeProgrammer's CLI. That CLI programs, resets and "
            "reads memory; it is not a debug server, so there is no session on this backend to hold a breakpoint, "
            "resume a core, or report why one stopped. `debug_start_session`, `debug_stop_session`, "
            "`debug_get_session_status`, `debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, "
            "`debug_continue`, `debug_halt` and `debug_get_stop_reason` are refused here for that reason, and so is "
            "`reset_target` with mode `init`, whose reset-init event script is an OpenOCD thing.\n\n"
            "What is *not* refused any more is the read half of the typed-debug family. `debug_symbol_info`, "
            "`debug_symbol_value` and `debug_dump_symbol_ihex` are served on this backend with no session behind them: "
            "the first resolves an address and a size out of the ELF `flash_firmware` put on the board and opens no "
            "probe at all, and the other two read the target with STM32CubeProgrammer's own memory read. Refusing a "
            "read this hardware can perform was the bug (#342): it sent a bench that wanted a RAM measurement away "
            "from its own probe. The two reads that do reach the board also connect hot plug now, because the connect "
            "they shipped with reset the target, and a read that resets destroys the RAM it was asked for.\n\n"
            "Nothing was sent to the bench for this refusal. The target is exactly as the last call that did reach it "
            "left it."
        ),
        remediation=(
            "First check whether a read answers the question. If what is wanted is a value out of the target (a "
            "counter, a coverage buffer, a status word, a structure), `debug_symbol_value` and "
            "`debug_dump_symbol_ihex` do that here without a session, and `debug_symbol_info` answers where a symbol "
            "lives without touching the board. They resolve against the ELF this service flashed, so flash the ELF "
            "with `flash_firmware` first and keep the symbol in `debug.allowed_symbols`.",
            "If the step genuinely needs a session (a breakpoint, a resume, a stop reason, stepping), the way out is "
            "a configuration change and not a different probe: the same ST-Link is a probe OpenOCD drives. Set "
            "`debuggers.<name>.type` to `openocd`, with `interface_cfg: interface/stlink.cfg` and the `target_cfg` for "
            "this part, `target/stm32f4x.cfg` for an STM32F4.",
            "The switch is one `project_config_set` call behind `allow_config_description_write`: send "
            "`debuggers.<name>.type` together with the fields the new backend requires, and it lands whole or is "
            "refused naming what is missing. Which debug stack a bench runs is the operator's decision, so report "
            "the change and get their word before making it. Afterwards the server adopts it through "
            "`project_config_reload_description` or a restart.",
            "Say what the move costs before it is made, because parts of this bench change hands with it. OpenOCD has "
            "to be installed and reachable, by PATH or `debuggers.<name>.executable`. A typed debug session is GDB, so "
            "`debug.gdb_executable` has to name a GDB that speaks this target, such as `arm-none-eabi-gdb`. A "
            "`connect_mode: under_reset` on that debugger has to go: OpenOCD refuses the value at load with "
            "`config_invalid`, and connecting under reset becomes a `reset_config` decision inside the interface and "
            "target scripts. And the part stops identifying itself: STM32CubeProgrammer reports the device name on "
            "every connect, while OpenOCD is told what the part is by `target_cfg`, so a wrong script fails to detect "
            "the target instead of adapting.",
            "If the bench has to stay on STM32CubeProgrammer, report the step as unavailable on this configuration and "
            "name which of the two halves was needed. A missing capability that is stated is a decision for the "
            "operator; one that is worked around quietly is a plan that reports something it did not do.",
        ),
        do_not=(
            "Do not read this as no debug on this bench. Three of the twelve typed-debug tools work here, and they are "
            "the three that answer what is in memory.",
            "Do not reach for `openocd`, `gdb`, `st-util` or a raw debugger command to get a breakpoint anyway. That "
            "bypasses the policy this refusal comes from, takes the probe out from under the bench's own coordination, "
            "and leaves the operator with no record of what ran.",
            "Do not swap the probe. The ST-Link is not what refused; the backend the configuration names for it is, "
            "and the same probe serves both.",
        ),
    ),
    "not_supported:pyocd": ErrorRemedy(
        meaning=(
            "This bench runs `type: pyocd`, which this server drives through pyOCD's command-line tools. pyOCD is not "
            "a debug server this server drives as one, so there is no session here to hold a breakpoint, resume a "
            "core, or report why one stopped. `debug_start_session`, `debug_stop_session`, `debug_get_session_status`, "
            "`debug_set_breakpoint`, `debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_continue`, "
            "`debug_halt` and `debug_get_stop_reason` are refused for that reason, and so is `reset_target` with mode "
            "`init`, whose reset-init event script is an OpenOCD thing.\n\n"
            "What is *not* refused any more is the read half of the typed-debug family. `debug_symbol_info`, "
            "`debug_symbol_value` and `debug_dump_symbol_ihex` are served here with no session behind them: the first "
            "resolves an address and a size out of the ELF `flash_firmware` put on the board and opens no probe at "
            "all, and the other two read the target with pyOCD's own `savemem`. Refusing a read this tool can perform "
            "was wider than the hardware's own limits (#344), the same gap #342 closed on the ST-Link backend. Those "
            "two reads connect with `--connect attach`, which is the one of pyOCD's four connect modes its own "
            "documentation describes as connecting to a running target without halting cores; the other three halt, "
            "reset before connecting, or hold reset asserted, and a read taken under any of them would report a "
            "disturbed board's bytes as a measurement.\n\n"
            "Nothing was sent to the bench for this refusal. The target is exactly as the last call that did reach it "
            "left it."
        ),
        remediation=(
            "First check whether a read answers the question. If what is wanted is a value out of the target (a "
            "counter, a coverage buffer, a status word, a structure), `debug_symbol_value` and "
            "`debug_dump_symbol_ihex` do that here without a session, and `debug_symbol_info` answers where a symbol "
            "lives without touching the board. They resolve against the ELF this service flashed, so flash the ELF "
            "with `flash_firmware` first, keep the symbol in `debug.allowed_symbols`, and have "
            "`debug.gdb_executable` name a GDB that reads that image.",
            "If the step genuinely needs a session (a breakpoint, a resume, a stop reason, stepping), the way out is "
            "a configuration change rather than different hardware: the probe this backend is driving "
            "is one OpenOCD drives too. Set `debuggers.<name>.type` to `openocd`, with the `interface_cfg` for the "
            "probe that is actually plugged in (`interface/stlink.cfg` for an ST-Link, `interface/cmsis-dap.cfg` for "
            "a CMSIS-DAP probe) and the `target_cfg` for this part.",
            "The switch is one `project_config_set` call behind `allow_config_description_write`: send "
            "`debuggers.<name>.type` together with the fields the new backend requires, and it lands whole or is "
            "refused naming what is missing. Which debug stack a bench runs is the operator's decision, so report "
            "the change and get their word before making it. Afterwards the server adopts it through "
            "`project_config_reload_description` or a restart.",
            "Say what the move costs before it is made. OpenOCD has to be installed and reachable, by PATH or "
            "`debuggers.<name>.executable`, and `debug.gdb_executable` has to name a GDB that speaks this target, "
            "because a typed debug session is GDB. `debuggers.<name>.target_type` stops being read: OpenOCD is told "
            "what the part is by `target_cfg`, so the CMSIS device-family pack that made pyOCD resolve the part is no "
            "longer what decides whether the bench works, and a wrong `target_cfg` fails to detect the target rather "
            "than adapting.",
            "If the bench has to stay on pyOCD, report the step as unavailable on this configuration and name which of "
            "the two halves was needed. A missing capability that is stated is a decision for the operator; one that "
            "is worked around quietly is a plan that reports something it did not do.",
        ),
        do_not=(
            "Do not read this as no debug on this bench. Three of the twelve typed-debug tools work here, and they are "
            "the three that answer what is in memory.",
            "Do not reach for `pyocd commander`, `pyocd gdbserver`, `gdb` or a raw debugger command to get a "
            "breakpoint anyway. That bypasses the policy this refusal comes from and takes the probe out from under "
            "the bench's own coordination.",
            "Do not swap the probe. The probe is not what refused; the backend the configuration names for it is.",
        ),
    ),
    # -- The debugger that is not a probe, in the two states it goes missing in --
    "gdb_not_found": ErrorRemedy(
        meaning=(
            "The GDB this bench names could not be run. `debug.gdb_executable` holds a path or a program name, and "
            "what it names is not there: no file at that path, or no such program on PATH. A typed debug session is "
            "GDB, and so is the offline symbol read the ST-Link and pyOCD backends answer out of the flashed ELF, so "
            "both refuse here. This is resolved before a debug server is started, so nothing was spawned and nothing "
            "was said to the target: the board is exactly as the last call that did reach it left it."
        ),
        remediation=(
            "Read the value this is about. `project_config_describe` names the authoritative file and reports "
            "`debug.gdb_executable` as it stands; a toolchain that was upgraded, moved or uninstalled is the usual "
            "cause, and the file is still naming where it used to be.",
            "Correct it with one `project_config_set` call behind `allow_config_description_write`. "
            "`debug.gdb_executable` takes an absolute path to a GDB that speaks this target (`arm-none-eabi-gdb` for a "
            "Cortex-M part, `gdb-multiarch` otherwise), or the bare program name when it is on PATH. Which GDB a bench "
            "runs is the operator's, so report the change and get their word before making it.",
            "Restart the MCP server afterwards. The GDB in force was resolved and validated when the server loaded its "
            "configuration, and `debug` is not one of the sections `project_config_reload_description` re-reads, so "
            "the running server keeps the value it started with until it starts again.",
        ),
        do_not=(
            "Do not run `gdb`, `arm-none-eabi-gdb` or `gdb-multiarch` yourself to get the answer anyway. That bypasses "
            "the policy this refusal comes from, takes the probe out from under this bench's coordination, and leaves "
            "the operator with no record of what ran.",
            "Do not point the key at a GDB inside the workspace. A configured executable has to live outside it, so a "
            "path within it is refused when the configuration loads and the bench stops starting at all.",
        ),
    ),
    f"gdb_not_found:{GDB_NOT_CONFIGURED_SCOPE}": ErrorRemedy(
        meaning=(
            "This bench has no GDB at all. The authoritative configuration leaves `debug.gdb_executable` unset, and "
            "none of `arm-none-eabi-gdb`, `gdb-multiarch` or `gdb` was on PATH when this server started, so the load "
            "recorded that there is none rather than refusing to start over a toolchain a project may not need. "
            "Nothing here is misconfigured and no path is wrong: there is nothing to run. A typed debug session is "
            "GDB, and so is the offline symbol read the ST-Link and pyOCD backends answer out of the flashed ELF, so "
            "both refuse until one exists. Nothing was spawned and nothing was said to the target."
        ),
        remediation=(
            "Install a GDB that speaks this target: `arm-none-eabi-gdb` for a Cortex-M part, `gdb-multiarch` "
            "otherwise. Either is found on PATH without the file naming it.",
            "If one is installed already but not on PATH, name it. `debug.gdb_executable` takes an absolute path and "
            "is one `project_config_set` call behind `allow_config_description_write`. Which GDB a bench runs is the "
            "operator's, so report what is missing and get their word before writing it.",
            "Restart the MCP server either way. The GDB a server uses is resolved and validated once, when it loads "
            "its configuration, and `debug` is not one of the sections `project_config_reload_description` re-reads, "
            "so a GDB installed or named under a running server reaches it at its next start.",
        ),
        do_not=(
            "Do not go hunting for a wrong path in the configuration. `debug.gdb_executable` names nothing here, and "
            "what the running server carries in its place is the record of that, not a path anybody wrote.",
            "Do not run `gdb`, `arm-none-eabi-gdb` or `gdb-multiarch` yourself to get the answer anyway. That bypasses "
            "the policy this refusal comes from and leaves the operator with no record of what ran.",
            "Do not report the bench as unusable. A probe with no GDB behind it still flashes, resets and probes; what "
            "stops here is the typed debug session and the symbol reads, and saying which of them was needed is what "
            "lets the operator decide whether installing one is worth it.",
        ),
    ),
    f"gdb_not_found:{GDB_AUTODETECTED_MISSING_SCOPE}": ErrorRemedy(
        meaning=(
            "This bench named no GDB, and the one it found is gone. The authoritative configuration leaves "
            "`debug.gdb_executable` unset, so this server autodetected `arm-none-eabi-gdb`, `gdb-multiarch` or `gdb` on "
            "PATH when it loaded and pinned the one it found; that file has since been moved or removed, and the pinned "
            "path no longer resolves. `project_config_describe` reports the key unset, because unset is what it is, "
            "nothing in the configuration is wrong and no path in it is stale. A typed debug session is GDB, and so is "
            "the offline symbol read the ST-Link and pyOCD backends answer out of the flashed ELF, so both refuse until "
            "one exists again. Nothing was spawned and nothing was said to the target."
        ),
        remediation=(
            "Reinstall the GDB that went missing, or install another that speaks this target: `arm-none-eabi-gdb` for a "
            "Cortex-M part, `gdb-multiarch` otherwise. Either is found on PATH without the file naming it.",
            "If one is installed already but not on PATH, name it. `debug.gdb_executable` takes an absolute path and is "
            "one `project_config_set` call behind `allow_config_description_write`. Which GDB a bench runs is the "
            "operator's, so report what is missing and get their word before writing it.",
            "Restart the MCP server either way. The GDB a server uses is resolved and validated once, when it loads its "
            "configuration, and `debug` is not one of the sections `project_config_reload_description` re-reads, so a "
            "GDB reinstalled or named under a running server reaches it at its next start.",
        ),
        do_not=(
            "Do not correct a path in the configuration. `debug.gdb_executable` names nothing here; the path that went "
            "missing was autodetected, not written, so there is no wrong value to fix and nothing `project_config_set` "
            "would be repairing.",
            "Do not run `gdb`, `arm-none-eabi-gdb` or `gdb-multiarch` yourself to get the answer anyway. That bypasses "
            "the policy this refusal comes from and leaves the operator with no record of what ran.",
            "Do not report the bench as unusable. A probe with no GDB behind it still flashes, resets and probes; what "
            "stops here is the typed debug session and the symbol reads.",
        ),
    ),
    # -- The one flag whose whole value is that it is never silently degraded ---
    CAN_INTERFACE_NOT_FOUND_ERROR: ErrorRemedy(
        meaning=(
            "The SocketCAN interface named by `can_buses.<name>.channel` does not exist on this host. The session was "
            "refused where the socket would have been bound, so no CAN controller was addressed and nothing was put on "
            "any bus: this is a refusal about the host's network configuration, not a quarantine. The usual cause is "
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
            "Do not run `recover --confirm-safe-state` over this. There is nothing to recover: no lease was "
            "quarantined, and signing for a physical state nobody disturbed teaches the signature to mean nothing.",
            "Do not point the entry at whichever `canN` happens to be up. That is the wrong-bus mistake the channel "
            "name exists to prevent; confirm which interface belongs to this bench first.",
        ),
    ),
    CAN_ADAPTER_LIBRARY_MISSING_ERROR: ErrorRemedy(
        meaning=(
            "The vendor library this CAN adapter is driven through is not installed on this host, so python-can "
            "refused before a driver object existed. Nothing was opened, no frame was sent, no bus state was read, "
            "and no controller ACKed anything: there was nothing for contact to happen through. This is a refusal "
            "about the host's software, in the same class as a missing toolchain, and the bench stays in service. "
            "For a `peak` bus the missing piece is the PCAN-Basic API, which is a separate vendor download from "
            "python-can and is not installed with `agentic-hil[can]`."
        ),
        remediation=(
            "Read `adapter` and `backend_error` on the result: they name which library was looked for and what the "
            "library layer said about it.",
            "For a `peak` bus, install the PCAN-Basic API from PEAK-System: the Windows device-driver setup ships "
            "`PCANBasic.dll`, Linux uses the `libpcanbasic` package, macOS the MacCAN `PCBUSB` library. Install it "
            "yourself as a deliberate host setup step; Agentic HIL names the dependency and never fetches it.",
            "For any other adapter, install the optional dependency that backend needs and confirm python-can can "
            "import it: `python -c \"import can; can.Bus(interface=...)\"` reports the same class of failure.",
            "`can_buses_list` reports every configured bus and its adapter, so which entry needs which library is "
            "readable before a session is started.",
            "Retry the session afterwards. The bench was never blocked: `retry_safe` is true and no incident was "
            "opened.",
        ),
        do_not=(
            "Do not run `recover --confirm-safe-state` over this. There is nothing to recover: no lease was "
            "quarantined, and signing for a physical state nobody disturbed teaches the signature to mean nothing.",
            "Do not switch the entry to another adapter to get past it. The adapter names the hardware that is "
            "attached, and a bus opened through the wrong backend is the wrong-bus mistake in a new spelling.",
        ),
    ),
    CAN_CHANNEL_NOT_AVAILABLE_ERROR: ErrorRemedy(
        meaning=(
            "The PCAN channel named by `can_buses.<name>.channel` is not a channel this driver has. "
            "`PCANBasic.Initialize` answered that the handle is invalid, so no channel was opened, nothing was put "
            "on the bus, and no controller ACKed: a channel the driver does not enumerate is the same provable "
            "innocence as a library that is not installed. The usual cause is that the dongle is unplugged, or that "
            "it re-enumerated onto a different `PCAN_USBBUSn` number than the configuration names."
        ),
        remediation=(
            "Read `channel` on the result: that is the channel name that was looked for. `available_channels`, when "
            "present, is what the driver actually enumerates right now.",
            "Check the adapter is attached and its driver is loaded. On Windows the PEAK tray tool lists attached "
            "channels; everywhere, `python -c \"import can; print(can.detect_available_configs([\'pcan\']))\"` asks "
            "python-can the same question this refusal asked.",
            "If the adapter is attached under a different number, put that number in the configuration: "
            "`project_config_set` writes it into `can_buses.<name>.channel`.",
            "Retry the session afterwards. The bench was never blocked: `retry_safe` is true and no incident was "
            "opened.",
        ),
        do_not=(
            "Do not run `recover --confirm-safe-state` over this. Nothing was quarantined and nothing on the bench "
            "moved; the channel was never opened.",
            "Do not point the entry at whichever channel happens to be attached. That is the wrong-bus mistake the "
            "channel name exists to prevent; confirm which adapter belongs to this bench first.",
        ),
    ),
    CAN_FD_REMOTE_FRAME_ERROR: ErrorRemedy(
        meaning=(
            "A remote frame was asked for on a bus configured `can_buses.<name>.fd: true`. Classic CAN's RTR bit, "
            "the one that marks a frame as a request rather than data, sits at the same position CAN FD's FDF bit "
            "occupies, and FDF is what tells a controller the frame is FD at all. A CAN FD controller therefore has "
            "no remote frame to send: the bit that used to mean one now means something else, and the ISO 11898-1 "
            "FD format carries no remote-frame encoding in its place. The request is refused before a frame is built "
            "out of it, rather than sent as whatever an FD frame with a stale RTR bit would come out as."
        ),
        remediation=(
            "Send this as a data frame instead. A device answering a request answers with data; if what you need is "
            "that answer, `rtr: false` with the expected reply's identifier and a normal `can_send` reads it once "
            "the device has put it on the bus.",
            "If a remote-frame poll is genuinely required against this device, declare a second `can_buses` entry "
            "for the same channel with `fd: false` and send the remote frame there: classic CAN still has RTR, and "
            "two entries keep the FD and classic intentions separately readable.",
            "`can_buses_list` reports `fd` for every configured bus, so which entries can carry a remote frame is "
            "readable before a plan is written.",
        ),
        do_not=(
            "Do not set `fd: false` on this bus just to get one remote frame out. Every frame after it reverts to "
            "classic CAN too, silently, until the entry is changed back.",
        ),
    ),
    CAN_CLASSIC_FRAME_TOO_LARGE_ERROR: ErrorRemedy(
        meaning=(
            "A payload longer than eight bytes was sent on a bus that is not configured `can_buses.<name>.fd: "
            "true`. Classic CAN's data field is eight bytes on every controller that speaks it; that is not a "
            "configured ceiling but the format itself, so `max_frame_data_bytes` being set higher (the schema used "
            "to allow up to 64 on a bus that never declared `fd: true`) could not have made this frame fit in one. "
            "The refusal is at the point a frame is actually built, which holds whatever `max_frame_data_bytes` a "
            "bus was loaded with before this guard existed."
        ),
        remediation=(
            "Send eight bytes or fewer on this bus.",
            "If this data belongs in one frame, set `can_buses.<name>.fd: true` (through `project_config_set`, or "
            "by asking the operator) on a bus whose adapter and controller actually support CAN FD, which turns on "
            "the sixteen lengths up to 64 bytes a single frame can carry.",
            "If the bus genuinely cannot be FD, split the payload across multiple classic frames at whatever "
            "protocol sits above raw CAN on this bus: that framing decision belongs above this server, which moves "
            "exactly the bytes it is given in each `can_send`.",
            "`can_buses_list` reports `fd` for every configured bus, so which entries can carry more than eight "
            "bytes is readable before a plan is written.",
        ),
        do_not=(
            "Do not raise `max_frame_data_bytes` to make this go away on a bus that is not `fd: true`. The schema "
            "holds it to eight there for exactly this reason, and even a hand-edited file that got past it would "
            "not change what a classic controller can put in one frame.",
        ),
    ),
    CAN_FD_FRAME_LENGTH_INVALID_ERROR: ErrorRemedy(
        meaning=(
            "A payload was sent on an `fd: true` bus whose length is not one CAN FD's DLC field can encode. The DLC "
            "nibble has sixteen codes: 0 through 8 count bytes one for one, and the remaining seven jump to 12, 16, "
            "20, 24, 32, 48 and 64, so a length between two of those, nine bytes or thirty for instance, names no "
            "code at all. Nothing here rounds a payload up and pads it to the nearest legal length on a caller's "
            "behalf: padding sent silently is data on the wire the caller never asked for, so a length outside the "
            "set is refused instead of adjusted."
        ),
        remediation=(
            "Send exactly one of the sixteen lengths CAN FD encodes: 0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, "
            "48 or 64 bytes: the result's `allowed_lengths` carries the same list.",
            "If the device accepts padded data, pad the payload to the next allowed length yourself before calling "
            "`can_send`, so the padding is on record in what was asked for rather than an adjustment made for you.",
            "`bytes_requested` on the result is the length that was refused, for checking against whatever produced "
            "it.",
        ),
        do_not=(
            "Do not raise `max_frame_data_bytes` in response to this. A length between two DLC codes is illegal at "
            "any ceiling; the fix is the length sent, not the bound it is checked against.",
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
            "If the bus may be ACKed (a bench nobody else is driving, a rig where this is the only participant), set "
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
            "configured and owned by the netdev, not by the socket this process opens. python-can's SocketCAN backend "
            "accepts a `listen_only` keyword and discards it, so there is nothing here that could apply the mode. It "
            "is read instead, and this interface is not in it, or its control mode could not be read at all, which is "
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
            "netdev, and an interface reconfigured mid-run is itself a change to the bus, which is what was being "
            "avoided.",
        ),
    ),
    LISTEN_ONLY_UNCONFIRMED_ERROR: ErrorRemedy(
        meaning=(
            "`listen_only: true` was requested, the adapter was asked for it, and it did not confirm the mode. The "
            "adapter was closed again, so the exposure is the open rather than a whole session, but it was on the bus "
            "for that long, and this result says so instead of implying otherwise. An unconfirmed listen-only is "
            "treated as no listen-only, because the value of the flag is the proof and not the request."
        ),
        remediation=(
            "Read `driver_state` on the result: it names what came back, which is what has to change.",
            "Check that the adapter has a listen-only mode at all. Not every CAN interface does; one that does not "
            "cannot be made to, and such a bus has to be observed with different hardware.",
            "`can_buses_list` reports `listen_only_enforcement` per adapter: what evidence would back the claim "
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
            "initialized (python-can applies it before `PCANBasic.Initialize` and discards the `SetValue` return "
            "code, so the constructor's request alone proves nothing), and then reads the parameter back. This channel "
            "did not read back as listen-only, so it was closed."
        ),
        remediation=(
            "Read `driver_state`. `reports PCAN_LISTEN_ONLY off` is the driver answering; anything about the parameter "
            "not being readable means the measurement failed rather than the mode.",
            "Confirm the PCAN hardware supports listen-only. PCAN-USB and PCAN-USB FD do; some OEM and older channels "
            "report the parameter as unsupported, and those cannot observe a bus without ACKing.",
            "Check that the PCANBasic driver and python-can are current (`python -m pip install --upgrade "
            "\"agentic-hil[can]\"`), since both the mode and the read-back go through PCANBasic.",
            "If the bench may be ACKed, set `listen_only: false` on that entry. Nothing else about the session "
            "changes.",
        ),
        do_not=(
            "Do not work around this by dropping `listen_only` and reading anyway on a bus carrying somebody else's "
            "traffic: a vehicle, a rig, hardware that is not yours. That is the exact case the flag is for.",
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
            "than a success without the field: the refusal then names the bridge's own reason instead of this one.",
            "If the bus may be ACKed, set `listen_only: false` on that entry; the request stops carrying the flag and "
            "the bridge is not asked to confirm anything.",
        ),
        do_not=(
            "Do not answer `listen_only: true` unconditionally to clear the refusal. That reintroduces the defect one "
            "process further out, where nothing in this repository can see it.",
        ),
    ),
    LISTEN_ONLY_MODE_ERROR: ErrorRemedy(
        meaning=(
            "A transmit was asked for on a CAN bus configured `can_buses.<name>.listen_only: true`, and was refused "
            "before the frame reached any driver. `listen_only` is a bus-level claim (that observing this bus sends "
            "nothing), and a controller held to it emits no dominant bit, so the frame could not have left it. This is "
            "not a permission: `permissions.allow_write` is never consulted, because a bus is not made "
            "transmit-capable by granting a permission on it. The refusal is the same on every adapter and through "
            "every route (the direct tool, a test plan's `can_send` step, a broker participant) because the flag "
            "describes the medium rather than the caller."
        ),
        remediation=(
            "If this bench really must not be disturbed, the send is the thing that is wrong. Drop it, or read the "
            "bus instead: `can_read` is what a `listen_only` bus is for.",
            "If some traffic must be transmitted and some observed, declare a second `can_buses` entry for the "
            "transmitting side (its own name, `listen_only: false`) and send on that one. Two entries make the two "
            "intentions separately readable, which one entry with a flag flipped mid-run never does.",
            "If the bus may be transmitted on after all, set `listen_only: false` on that entry. That is an honest "
            "configuration and the refusal goes away, because nothing is being claimed any more.",
            "`can_buses_list` reports `listen_only` and `listen_only_enforcement` for every configured bus, so which "
            "buses will refuse a transmit is readable before anything is started.",
        ),
        do_not=(
            "Do not grant `permissions.allow_write` in the hope of clearing this. The mode is settled first and the "
            "permission is never read; adding it only widens what the config allows without changing this answer.",
            "Do not flip `listen_only: false` on a bus carrying somebody else's traffic (a vehicle, a rig, hardware "
            "that is not yours) merely to get one frame out. That is the exact case the flag is for, and the "
            "controller would begin ACKing every frame on the medium, not only the one being sent.",
            "Do not reach past Agentic HIL to a python-can script or `cansend` to transmit anyway. On PEAK, "
            "python-can's `send()` does not consult the bus state at all: it hands the frame to `PCANBasic.Write` and "
            "reports success for queue acceptance, so a script would answer `sent` and tell you nothing about the "
            "wire.",
        ),
    ),
    "can_adapter_protocol_unsupported": ErrorRemedy(
        meaning=(
            "A CAN process bridge answered its `open` request with something this protocol does not accept: a "
            "`protocol_version` that is not the one this server speaks, a field outside the response's closed set, or "
            "a `backend`, `summary` or `listen_only` of the wrong type. The bridge is running and did answer, so the "
            "fault is the shape of the answer and not the transport. The session is refused rather than opened on a "
            "response nobody can read."
        ),
        remediation=(
            "Make the bridge's `open` result carry `\"ok\": true` and `\"protocol_version\": 2`, and nothing beyond "
            "`ok`, `protocol_version`, `backend`, `summary` and `listen_only`. `backend` and `summary` are strings "
            "where present and `listen_only` is a boolean.",
            "The set is closed on purpose: an unrecognised field is how a bridge speaking a later or a private "
            "protocol would otherwise pass as one speaking this one. Carry extra detail in `summary`.",
            "A bridge that cannot open the bus should answer `\"ok\": false` with its own `error_type` and `summary`. "
            "That refusal reaches the caller as the bridge's own reason, which is more useful than this one.",
            "`can_buses.<name>.adapter: process` selects this transport; check that the configured command is the "
            "bridge that was meant and not another program that answers on stdout.",
        ),
        do_not=(
            "Do not treat this as a bus or a wiring fault. Nothing was read off the bus and no frame was sent; what "
            "failed is the agreement between this server and the bridge process.",
            "Do not silence it by widening what the bridge sends. A response that is accepted because the check was "
            "relaxed is a response nobody has checked.",
        ),
    ),
    "can_adapter_invalid_response": ErrorRemedy(
        meaning=(
            "The CAN adapter answered a `send`, `read` or `open` request with a payload this server cannot read: a "
            "result outside the closed field set for that method, a wrong type where the protocol fixes one, or frame "
            "data that does not decode. Because the request was delivered before the answer came back, whether the "
            "bridge acted on it is unknown: the result carries `side_effect_status: unknown` and "
            "`cleanup_required: true`, and a frame may or may not have reached the bus."
        ),
        remediation=(
            "Read the bus state from the target itself before sending anything else: the pending frame may have gone "
            "out. The refusal deliberately does not guess.",
            "Close the session with `can_session_stop` and open it again. A bridge that answered one request "
            "unreadably has no state this server can rely on for the next.",
            "Fix the bridge's response shape: `send` answers `ok`, and optionally `backend` and `summary` as strings; "
            "`read` adds `frames` as a list, each frame carrying `id`, `extended`, `rtr`, `data_hex` and a `dlc` that "
            "matches the decoded byte count.",
            "If the adapter is not a bridge, the malformed frames came from the CAN library itself. Check the "
            "adapter's driver and firmware version against what `can_buses_list` reports for that bus.",
        ),
        do_not=(
            "Do not resend the frame on the assumption that it did not go out. Duplicating a stimulus onto a live bus "
            "is the specific outcome the unknown status exists to keep you from choosing blind.",
            "Do not carry on with the open session. Whatever the bridge is doing with its channel, this server no "
            "longer has a reliable account of it.",
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


def command_line_remediation(error_type: str | None, scope: str | None, steps: list[str]) -> list[str] | None:
    """``steps`` reordered for a person at a shell, or None when nothing moves.

    The catalogue decides this, not the renderer: which reader a step is for is
    part of what the step says, and a rendering layer that reordered advice by
    its own rule would be a second source of truth about it.

    None means "print what the document carries", and it is the answer to three
    different questions on purpose: this error has no separate ordering for a
    person; there is no catalogue entry at all; or the steps in hand are not
    this entry's, because a caller built its own list or added to one. The last
    is what the multiset comparison is for. Reordering a list this entry does
    not account for would drop or duplicate somebody's advice, and the one thing
    a renderer may never do to a refusal is change what it says.
    """
    remedy = lookup_remedy(error_type, scope)
    if remedy is None or not remedy.cli_remediation:
        return None
    values = _substitutions()
    ordered = [step.format(**values) for step in remedy.cli_remediation]
    return ordered if sorted(ordered) == sorted(steps) else None


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
    # on the machine as much as what to do about it does (whether the discovered
    # default under %APPDATA% is itself the case being described depends on this
    # host's join state), and a placeholder that reached a reader verbatim would be
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
    impossible (the justification for needing a human at all), and
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
    "Whether the actions since the last committed record reached the device, and in what order: the evidence channel "
    "itself is what broke, so no later record can answer this."
)
_AUDIT_PHYSICAL_CHECK = (
    "Fix the audit destination first (free disk space, permissions on the reports and logs directories under "
    "state_root), read the last committed report with `agentic-hil` `get_last_report`, confirm the board matches what "
    "it describes (firmware, running/halted, wiring), and only then sign."
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
        unknown="Nothing about the board: what is missing is the durable record; until it exists, later readers cannot distinguish this from a read that ended badly.",
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
        unknown="Whether the device is in the state the last operation intended: the holder explicitly declined to confirm it.",
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
    "run_recovery_failed": QuarantineReasonGuide(
        attempted="A run failed, and the recovery action its abort calls (process reap, a reset into halt where the policy and the probe's grants allow it, then a probe re-read) raised instead of finishing.",
        confirmed="The run's own verdict stands and its reports are written. Nothing after the raise touched the device.",
        unknown="How far the recovery got: whether the reset reached the target before it failed, and therefore whether the board is halted, running the code the failed run left on it, or somewhere between.",
        physical_check="Treat the board as the failed run left it. Reset it by its own controls, confirm it holds the firmware you expect, then sign.",
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
        attempted="A read-only probe call (probe discovery or probe_target) named an abort point before the target and still returned a result the host could not settle: an unknown or partial side effect, or cleanup left outstanding.",
        confirmed="The backend's own report says the target was never contacted (`target_contacted: false`), so the board keeps the state the last effectful call left.",
        unknown="Why a call that never reached the target reported an effect at all; a read-only re-read settles it, which is why this reason is machine-recoverable.",
        physical_check="Normally none: the next hardware call re-reads the probe and clears this itself. If the probe stays unreachable, reseat it, then sign.",
    ),
    "debugger_readonly_target_state_unconfirmed": QuarantineReasonGuide(
        attempted="A read-only probe call (probe discovery or probe_target) failed without naming where it stopped: the backend was killed at its deadline, or it reported a failure that does not place the abort point before the target.",
        confirmed="The toolchain child process was reaped, so nothing from this call can still act on the board.",
        unknown="Whether the read reached the target before it stopped. A read on this bench is not passive (an SWD attach halts the core), and a process killed at its deadline never ran the shutdown in its own command string, so the core may be sitting halted with nothing to resume it.",
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
        unknown="Whether the toolchain child process was reaped and what it sent before the exception: a returned result would have proven the child was terminated, an exception proves nothing.",
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
        unknown="Whether hardware breakpoints remain armed on the target: firmware run under a leftover breakpoint stops where nobody expects.",
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
        unknown="Whether the OS handle was left open and whether the modem lines (DTR/RTS) were left asserted: on a board that wires DTR to reset, that holds the target in reset.",
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
        unknown="How many of the requested bytes reached the wire: the target may have received a truncated command.",
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
        unknown="Whether the adapter channel is still initialized: a channel brought up at the wrong bitrate disturbs the bus just by listening.",
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
        unknown="Whether the frame reached the bus: receivers may have acted on it.",
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
    """Attach the per-reason guidance to a result that names a cleanup reason.

    Applied at reporting time, never persisted: records and audit reports keep
    only the reason strings, so catalogue text can improve between versions
    without stale copies surviving in state files.

    Keyed off the reason rather than off the gate. Since the quarantine narrowed
    to the audit families, most reasons name something a call could not confirm
    without the bench being held for it, and what was attempted, what still
    holds, what stays unknown and what to check on the board is exactly as
    useful then as it was when a person had to sign for it. So a result that
    carries a cleanup reason carries its guidance, blocked or not."""
    if result.get("quarantined") is not True and result.get("cleanup_required") is not True and not result.get("cleanup_reasons"):
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
        "type": {"status": "optional", "value": "openocd", "note": "Default. Omit only if no other backend is meant. Settable over MCP behind allow_config_description_write, and switching an entry to this backend has to carry interface_cfg and target_cfg in the same call, because OpenOCD reaches the board through no other route; an entry that does not name them is refused rather than left half switched. Send executable in that call too, or `null` to have OpenOCD discovered on PATH: an executable already in the entry was chosen for the backend the entry is leaving."},
        "executable": {"status": "discovered", "note": "Falls back to `openocd` on PATH, except on the untouched starter entry, which stays inert until somebody names a toolchain in it. An absolute path or a value containing a separator is resolved against workspace_root and must exist."},
        "probe_id": {"status": "optional", "note": "Adapter serial number, passed as `adapter serial <probe_id>`. Required once more than one debugger is configured."},
        "target_type": {"status": "ignored", "note": "OpenOCD selects the target through target_cfg."},
        "interface": {"status": "ignored", "note": "OpenOCD selects the transport through interface_cfg."},
        "interface_cfg": {"status": "required", "default": "interface/stlink.cfg", "note": "OpenOCD script, passed as `-f`. Either an OpenOCD search name such as `interface/stlink.cfg`, which OpenOCD resolves against its own script path and which therefore does not have to exist on this host, or an absolute path to an existing file outside the workspace. A path under the system temporary directory is refused: it is cleared without warning and the configuration would stop describing this bench."},
        "target_cfg": {"status": "required", "default": "target/stm32f4x.cfg", "note": "OpenOCD script, passed as `-f`, a search name or an absolute path outside the workspace like interface_cfg. Must match the MCU family."},
        "connect_mode": {"status": "refused", "default": "hotplug", "enum": ["hotplug"], "note": "OpenOCD reaches the target through the scripts named above, and connecting under reset is a `reset_config` decision inside them that depends on how SRST is wired for this adapter and this part. `under_reset` is therefore refused at load here rather than accepted and ignored; ask for the same effect in interface_cfg or target_cfg."},
        "flash_address": {"status": "ignored", "note": "OpenOCD takes the load address from the image."},
    },
    "stlink": {
        "tool": "STM32_Programmer_CLI (STM32CubeProgrammer)",
        "type": {"status": "required", "value": "stlink", "note": "Settable over MCP behind allow_config_description_write. Switching an entry to this backend needs no other key of this surface: interface defaults to SWD, and interface_cfg and target_cfg are ignored here, so they may stay in the entry. Send executable in the same call, or `null` to have STM32_Programmer_CLI discovered: an executable already in the entry was chosen for the backend the entry is leaving."},
        "executable": {"status": "discovered", "note": "Falls back to STM32_Programmer_CLI on PATH, then the standard STM32CubeProgrammer and STM32CubeIDE install locations."},
        "probe_id": {"status": "optional", "note": "ST-Link serial number, passed as `sn=<probe_id>`. Required once more than one debugger is configured."},
        "target_type": {"status": "ignored", "note": "STM32CubeProgrammer identifies the part itself."},
        "interface": {"status": "required", "default": "SWD", "enum": ["SWD", "JTAG"], "note": "Passed as `port=<interface>`."},
        "interface_cfg": {"status": "ignored"},
        "target_cfg": {"status": "ignored"},
        "connect_mode": {"status": "optional", "default": "hotplug", "enum": ["hotplug", "under_reset"], "note": "How the probe attaches for a flash, passed as `mode=HOTPLUG` or `mode=UR` on the connect. `hotplug` connects to the running core and is what a file that does not name this key does. `under_reset` holds the target in reset for the connect, which is the fix for a first flash that fails at erase and succeeds on the retry: a core executing from flash defeats the erase it is left running through. It needs the probe's reset line wired to NRST, because STM32CubeProgrammer pairs UR with a hardware reset. Read by flash_firmware alone: probe_target stays hot plug so it remains the least intrusive call, and reset_target keeps its own NORMAL connect."},
        "flash_address": {"status": "conditional", "note": "Required to flash a .bin, which carries no load address. Not read for .elf or .hex. Pattern: 0x-prefixed hex or decimal, e.g. 0x08000000."},
    },
    "pyocd": {
        "tool": "pyocd",
        "type": {"status": "required", "value": "pyocd", "note": "Settable over MCP behind allow_config_description_write. Switching an entry to this backend needs no other key of this surface, because target_type is not one it writes and pyOCD guesses from the probe's board ID when it is unset; a bench that needs a specific part still has to have target_type in the file. Send executable in the same call, or `null` to have pyocd discovered: an executable already in the entry was chosen for the backend the entry is leaving."},
        "executable": {"status": "discovered", "note": "Falls back to `pyocd` on PATH. Install with `pip install agentic-hil[pyocd]` or `pip install pyocd`."},
        "probe_id": {"status": "optional", "note": "Probe unique ID, passed as `--uid`. pyOCD matches it as a case-insensitive substring and strips a leading `<type>:`, so give the full ID. Required once more than one debugger is configured."},
        "target_type": {"status": "required", "note": "Passed as `--target`. Omitted entirely when unset, leaving pyOCD to guess from the probe's board ID. Most vendor parts resolve only after a CMSIS pack is installed."},
        "interface": {"status": "ignored"},
        "interface_cfg": {"status": "ignored"},
        "target_cfg": {"status": "ignored"},
        "connect_mode": {"status": "refused", "default": "hotplug", "enum": ["hotplug"], "note": "Nothing this key could say reaches pyOCD, so `under_reset` is refused at load rather than accepted and ignored. A bench that needs the flash to connect under reset runs it on `type: stlink`. The one connect option this server does pass to pyOCD is not this key's: the typed-debug memory reads send `--connect attach`, fixed, because it is the only mode pyOCD documents as reaching a running core without halting or resetting it."},
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

# A second, later rule than MULTI_PROBE_RULE above, and deliberately not
# merged into it: that one is what config load rejects about the *text* of a
# multi-probe document (two entries naming one probe), and is enforced with
# error_type config_invalid before any tool exists to call. This one is what
# a probe-addressing tool call itself refuses about the *bound* entry once
# several are configured, with error_type not_supported, and it is the one
# place a document with several unnamed debuggers is allowed to load at all:
# discovering the ids is exactly what an operator does before they can
# write one down, which config load cannot ask of them.
# What `agentic-hil init`, `agentic-hil debugger-probes`, `agentic-hil
# adopt-hardware` and `project_config_create` do before any of the entries above
# exists. Stated here rather than only in the per-backend matrix, because it is a
# fact about the host rather than about a configured entry: which of the two
# enumerations answered decides what the generated entry's `type` and
# `executable` will be.
BOOTSTRAP_DISCOVERY_RULE = {
    "rule": (
        "Bootstrap discovery has two enumerations, and the host decides which runs: STM32CubeProgrammer's "
        "STM32_Programmer_CLI where it is installed, and otherwise this host's own USB serial inventory with OpenOCD "
        "as the toolchain."
    ),
    "stm32cubeprogrammer_cli": (
        "`-l st-link-only` lists the probes and a HOTPLUG connect reads the part number off the target. The generated "
        "entry is `type: stlink` with that CLI as its `executable`."
    ),
    "usb_serial_inventory": (
        "A host serial port whose USB vendor is 0483 and whose product is one of the ST-Link ids publishes the probe "
        "serial in its descriptor, which is the string OpenOCD's `adapter serial` takes. The toolchain is the "
        "`openocd` on PATH, and the generated entry is `type: openocd` with its interface_cfg and target_cfg. This is "
        "the path on an ordinary Linux workstation, which normally has OpenOCD and not STM32CubeProgrammer."
    ),
    "usb_serial_inventory_is_not_a_complete_count": (
        "This inventory reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone "
        "ST-LINK/V2 -- or any probe with no VCP -- can be attached and never appear: `complete: false`, an empty "
        "reading is not proof no probe is connected, and a sole visible one is not proof it is the only one. So the "
        "generated OpenOCD entry is bound only on an explicit selection, never inferred from this count alone: name the "
        "serial as probe_id (which `select_probe_id` still checks against the inventory), or install STM32CubeProgrammer "
        "for an authoritative count. Without one of those, discovery refuses `probe_inventory_incomplete`: "
        "`project_config_create` writes nothing and `agentic-hil init` writes an unbound placeholder rather than "
        "committing a board a later flash or reset would trust."
    ),
    "target_identity_without_the_cli": (
        "The workspace profile's `target.controller` when it names one, which is exact and says nothing to the board; "
        "otherwise a read-only OpenOCD `init`, `targets`, `shutdown` against the selected adapter serial, which "
        "reports the target script's family rather than the part number. Neither flashes, erases, resets nor halts. A "
        "probe whose target could not be named is still written down, with `target.controller` left at the "
        "placeholder."
    ),
    "reported_as": (
        "`discovered_by` says which enumeration answered (`stm32cubeprogrammer_cli` or `usb_serial_inventory`), "
        "`tools_searched` says which binaries were looked for and where each resolved, and `stlink_ports` lists the "
        "ST-Link serial ports this host is showing."
    ),
    "neither_installed": (
        "error_type `debugger_not_found`, naming both tools. OpenOCD alone is enough and is the smaller install; "
        "STM32CubeProgrammer additionally reads the part number off the target."
    ),
    "ambiguity": (
        "Unchanged either way: more than one attached ST-Link is `ambiguous_hardware`, and a requested probe_id "
        "selects among what was enumerated and never adds to it."
    ),
}

UNNAMED_PROBE_RULE = {
    "rule": "Once `debuggers` holds more than one entry, the bound one must carry a probe_id before a probe-addressing tool (flash_firmware, reset_target, probe_target, the typed debug tools) will drive it.",
    "why": "the bound entry's name alone does not prove which physical probe a call reaches once another configured entry could just as easily be meant; probe_id is what pyOCD and ST-Link verify against the attached hardware, and what OpenOCD opens by adapter serial.",
    "enforced_at": "each probe-addressing tool call, as error_type `not_supported`",
    "single_debugger_exemption": (
        "A lone configured debugger does not have to carry a probe_id: it has no other entry to be confused with, so "
        "the rule above would not remove any ambiguity there, only block the bench outright - including the "
        "Nucleo-F446RE + ST-Link + OpenOCD bench this project documents as its supported first path, for which OpenOCD "
        "has no probe listing of its own: debugger_probes_list answers there out of this host's USB serial inventory, "
        "which names an ST-Link and no other adapter, so an OpenOCD entry on any other probe still has no serial it "
        "could be made to carry. And a probe with no serial pyOCD or ST-Link can read either - the debugger analogue "
        "of the CH340-style adapters com_ports already has to tolerate - would have no way to satisfy it at all. The exemption is from being "
        "forced to, not from being able to: probe_id still works, and is still checked against the attached hardware, "
        "with exactly one debugger configured."
    ),
    "what_the_exemption_does_not_cover": (
        "whether the one probe behind an unnamed single debugger is still the physical unit it was last run. Nothing "
        "here, or at the pyOCD/ST-Link/OpenOCD boundary, pins that without a probe_id; the coordination lock has the "
        "same blind spot for the same reason (see devices.DebuggerDevice.identity_warning)."
    ),
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
            "refused": "this backend has no equivalent, so the values it cannot carry out are rejected at load instead of being read and ignored; `enum` lists what it does accept",
        },
        "backends": DEBUGGER_FIELD_MATRIX,
        "bootstrap_discovery": BOOTSTRAP_DISCOVERY_RULE,
        "probe_id_when_multiple_probes": MULTI_PROBE_RULE,
        "probe_id_at_tool_call_time": UNNAMED_PROBE_RULE,
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
                    "Version 1 only. A configuration that sets `version: 2` or higher must not carry it; a "
                    "configuration without a `version` key is still read under version 1, where reading needs it. Both "
                    "COM ports and CAN buses lost `permissions.allow_read` the same way."
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
    hand a node onward copy it (``config_key_schema`` does), so nothing that
    escapes into a result can write back into the cache."""
    document = json.loads(config_schema_text())
    if not isinstance(document, dict):  # pragma: no cover - the shipped schema is an object
        raise ValueError("The bundled configuration schema is not a JSON object.")
    return document


def plan_schema_text() -> str:
    return resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def plan_schema_document() -> JsonObject:
    """The shipped test plan schema, parsed once.

    Cached for the reason the configuration schema is: the reactor validates
    every plan against it and the reference document below is generated from it,
    while the file itself is packaged data that cannot change while the process
    runs. ``test_reactor.test_config_schema`` returns this same object, so the
    document a plan author reads and the schema a plan is refused by are one
    thing. Callers must not mutate it.

    Not named ``test_plan_schema_document``: pytest collects a module-level name
    beginning with ``test_`` the moment a test module imports it, and the test
    that pins this document against the schema does exactly that."""
    document = json.loads(plan_schema_text())
    if not isinstance(document, dict):  # pragma: no cover - the shipped schema is an object
        raise ValueError("The bundled test plan schema is not a JSON object.")
    return document


# ---------------------------------------------------------------------------
# Which configuration keys an agent may set over MCP, and which grant opens each.
#
# Two rights, not one. One right for everything would be a master key: whoever
# set it so an agent could enter a 24-character probe serial would have handed
# over, in the same motion and without being told, the ability for that agent to
# write `allow_flash: false` on a bench somebody else was about to flash.
#
#   description  what the bench IS:    target, probe identity, port parameters
#   permissions  what the bench MAY:   every permission key in the file: each
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
    passing and the operator is in front of a file they have to open by hand:
    the exact state the open generated default exists to end.

    Three things, because three are what a reader needs: what stands frozen now,
    that the agent itself cannot undo it, and the name of the command that can.

    Said about `project_config_set` and about nothing else, because that is the
    whole of what this closes. Regeneration is a different call under a
    different grant and it is creation rather than a permissions write: the
    owner's decision behind the open generated default keeps it out of the
    ratchet deliberately. A notice that claimed the whole file was sealed would
    be describing a rule this project does not have.
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
            "which of them were left granted: `frozen_permissions` is that list, read out of the file as written.",
            f"You cannot undo it with `{PROJECT_CONFIG_SET_TOOL}` and there is no permission that would let you. Do not "
            "call it on a permission again.",
            f"A person opens one permission again with `{CONFIG_GRANT_COMMAND} <key>` in the project root (including "
            f"`{CONFIG_GRANT_COMMAND} permissions.{CONFIG_PERMISSIONS_RIGHT}`, which is what unfreezes this), and it "
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
    does not need a second edit here to become settable, and cannot be silently
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
    # rest of those entries stays locked on its own merits, each of them a
    # setting that changes what a call does to the board while describing
    # nothing about the board: `flash_address` decides where an image lands,
    # `resource_id` renames the lock a run takes and can hand one probe's
    # exclusivity to another entry, `timeout_s` decides when a call is abandoned
    # mid-operation, and the COM buffer limits and DTR/RTS lines decide how much
    # of a line is read and whether opening a port restarts the target. None of
    # those is what an attached probe hands you either.
    ConfigKeyRule("target", named=False, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT),
    # `type` used to be on that locked list, and the reason given for it was the
    # same sentence: it changes what a call does to the board. That reason does
    # not survive next to the three fields standing beside it. `executable`,
    # `interface_cfg` and `target_cfg` between them already decide which binary
    # runs with which scripts, so an operator who grants description-write has
    # handed over what reaches this board whichever way `type` reads; and which
    # debug stack a bench runs is a description of that bench, learned the way
    # every other description here is learned, from the hardware in front of
    # somebody. Leaving it out cost exactly what the split exists to prevent: a
    # bench that had to switch from CubeProgrammer to OpenOCD left MCP and
    # edited its own configuration by hand (#343).
    #
    # What keeps the switch honest is validation rather than a lock. `type` is
    # the one description key whose value decides which *other* fields the entry
    # needs, so a change that names a backend the entry is not equipped for is
    # refused naming exactly what is missing, and the call lands a whole entry
    # or changes nothing.
    #
    # `connect_mode` joins them under the same right. It is not a
    # permission and it widens nothing: the two values it takes are both a flash
    # this configuration already allows, and the difference between them is
    # whether the target is held in reset while the probe attaches. What it
    # describes is a property of this board, that its core runs from flash on
    # power-up and defeats an erase it is left running through, and a bench
    # learns that from a first flash that failed and a retry that worked. Behind
    # the permissions grant it would sit with the keys that decide authority,
    # where nobody could set it without also being able to grant themselves
    # flashing.
    ConfigKeyRule("debuggers", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("type", "probe_id", "executable", "interface_cfg", "target_cfg", "connect_mode")),
    # `serial_number` is in the description half for the same reason `probe_id`
    # is: it is what an attached board hands you, and it says which unit this
    # entry is rather than what may be done to it. `vid`/`pid` come off the same
    # enumeration record and say which *kind* of device it is, which is what
    # makes the serial mean a unit at all. `identity_source` is in it because it
    # is decided by those three and by nothing else (it grants nothing, and a
    # value disagreeing with them is refused at load), and because
    # `adopt-hardware` has to be able to write it: whether an adapter publishes a
    # serial number at all is a fact only a read of the hardware settles, and
    # version 3 requires the file to state it.
    ConfigKeyRule("com_ports", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("device", "baudrate", "serial_number", "vid", "pid", "identity_source")),
    ConfigKeyRule("can_buses", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT),
    # And the one description key the `debug` section carries. Which GDB reads
    # this bench's images is the same class of fact as
    # `debuggers.<name>.executable`: a toolchain on this host, named in the file
    # because only this host knows where it is. It grants nothing, every tool
    # that reads it is gated by the debug permissions beside it, and a bench
    # whose GDB lives off PATH had no sanctioned way to say so: generation
    # writes `null` whenever the generating shell had none, adoption carried
    # probe identity only, and this surface refused the key. What was left was
    # hand-editing the authoritative file, the move the doctrine tells agents
    # never to make and tells operators they should not need (#355).
    #
    # `debug` is therefore the one section with a key on each side of the split,
    # which is why both of its rules name their fields explicitly: a dotted key
    # resolves against the rule whose fields contain it, so `gdb_executable`
    # lands on the description right and `allow_all_symbols` on the permissions
    # right, out of one model rather than two.
    ConfigKeyRule("debug", named=False, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("gdb_executable",)),
    # The permissions half, every block of it.
    ConfigKeyRule("permissions", named=False, under_permissions=False, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("debuggers", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("com_ports", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    ConfigKeyRule("can_buses", named=True, under_permissions=True, right=CONFIG_PERMISSIONS_RIGHT),
    # And the two grants the schema puts directly on a fixed section instead of
    # inside a `permissions` block. A generation writes both true like every
    # other permission, so leaving them out of the key model left two things a
    # generated bench grants that an operator could only take back by opening the
    # YAML: the one thing the ratchet exists to stop. Only the grant of each
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


def _writes_a_scalar(schema: JsonObject, node: object) -> bool:
    """Whether this schema node holds a value one ``project_config_set`` can carry.

    An object or an array is a subtree, and a subtree set through this surface is
    content the agent authored rather than a value an operator chose, which is
    the one thing the key model exists to prevent. The rules below used to keep
    that promise by hand, listing ``fields`` explicitly wherever a section had
    grown a list; a section that grew one later simply became settable, silently.
    Reading it off the schema is the same promise made structurally."""
    declared = _dereference(schema, node).get("type")
    kinds = set(declared if isinstance(declared, list) else [declared])
    return not kinds & {"object", "array"}


@cache
def config_rule_fields(rule: ConfigKeyRule) -> tuple[str, ...]:
    """The field names this rule covers, read out of the shipped schema.

    An explicit subset is returned as written. A rule that names none covers
    everything the schema declares for that node except ``permissions`` (which
    belongs to the other right), except keys the schema marks deprecated
    (``allow_probe`` and ``allow_read`` exist only for version 1 files and are
    refused outright in a version 2 one) and except values that are not
    scalars, because this surface sets one value at a time and a subtree is not
    one. ``can_buses.<name>.shares`` is the standing example: participant views
    are an operator's structure, edited in the file, not a key an agent sets."""
    if rule.fields:
        return rule.fields
    schema = config_schema_document()
    properties = _section_entry_schema(schema, rule).get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(
        sorted(
            name
            for name, node in properties.items()
            if name != "permissions"
            and not (isinstance(node, dict) and node.get("deprecated") is True)
            and _writes_a_scalar(schema, node)
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
    which makes the last component (or the last ``.permissions.<flag>`` pair)
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
# the write path below is operable only by guessing (write, be refused, guess
# the next key), and every guess costs a round trip.
#
# So this document explains, and takes every value shape from the schema at read
# time. Two descriptions of one permission boundary drift, and the configuration
# is the permission boundary.

# What the schema cannot say about a section: why it is there, and which other
# resource already answers the per-field questions in depth. Keyed by section and
# merged with the schema's own description rather than replacing it. Where the
# schema says nothing about a field, this document says nothing either, because
# the alternative is a second description of the same field.
_SECTION_PURPOSE: dict[str, str] = {
    "version": "Which rules the file is read under. Versions 1, 2 and 3 all load, so no file already on disk has to move. A new file should say `3`.",
    "workspace_root": "The project this configuration authorizes, and nothing else. A server started elsewhere refuses it.",
    "state_root": "Where leases, quarantine incidents and canonical reports live. Outside `workspace_root`, so repository content cannot forge them.",
    "permissions": "What may be done to this project beside its hardware: to this file itself, and to a quarantine incident on this bench.",
    "provenance": "Who wrote this file and who last changed it. A note to a reader; nothing reads it as policy.",
    "target": f"What board this is. Names in reports; `controller` is what a human recognises. Which field actually selects a target per backend, and known-good values: {TARGET_SUPPORT_URI}.",
    "debuggers": f"The debug probes. The entry name is the routing key a test plan addresses. `type` names the debug stack that drives the entry and is settable like the rest of the description, but only as a whole switch: a change to it has to arrive with whatever the backend it names requires, or it is refused naming what is missing. Which of these fields each backend requires, discovers, ignores or refuses, `type` and `connect_mode` included: {DEBUGGER_BACKENDS_URI}.",
    "debug": (
        "Typed GDB session settings: which GDB reads this bench's images, which symbols may be read and how much. "
        "`gdb_executable` is the description half of this section and is settable behind "
        f"`{CONFIG_DESCRIPTION_RIGHT}`; `allow_all_symbols` is a grant and belongs to the other right."
    ),
    "artifacts": "Which firmware files may be flashed, from where, and how large.",
    "com_ports": "The serial lines. `device` is how a port is opened and `serial_number` is which board it is. Name both, because a kernel name like `/dev/ttyACM0` or `COM7` is an enumeration order and moves when another adapter is attached. `vid`/`pid` name which kind of adapter it is, which is what makes a serial mean a unit at all and is the only identity an adapter that publishes no serial can have. From `version: 3` on an entry must say which of them identifies it: a `serial_number`, a `resource_id` or a `/dev/serial/by-id/...` device name, or else an explicit `identity_source` (`vid_pid` for an adapter publishing USB ids but no serial, `device` for one publishing neither). Reading needs no permission; `assert_dtr`/`assert_rts` decide whether opening one restarts the target.",
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

CONFIG_WORKED_EXAMPLE = """version: 3

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
  # contact with no argument, and may clear one that needs somebody at the board
  # only by relaying an operator_statement a person gave it, never invented.
  allow_recover: true

target:
  name: "thermostat-dut"
  controller: "stm32f446re"

debuggers:
  dut:
    type: "openocd"
    executable: null            # resolved from PATH when this file is loaded
    probe_id: "066AFF495451885087171450"
    # Spelled as paths here, so this bench names the exact scripts it runs
    # rather than whatever OPENOCD_SCRIPTS and the per-user script directories
    # resolve on the day. A path is checked as one: absolute, outside
    # workspace_root, an existing file, and never under the system temporary
    # directory. The other spelling is `interface/stlink.cfg`, OpenOCD's own
    # search name, which the installed OpenOCD resolves and this file does not
    # promise a location for.
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
    device: "COM7"              # the ST-Link virtual COM port: how it is opened
    # Which board it is. Version 3 requires this, a resource_id, or a
    # /dev/serial/by-id/... device name: COM7 is an enumeration order, so it can
    # come to mean the other adapter. An adapter that publishes no serial says so
    # instead, with identity_source: vid_pid, or identity_source: device when it
    # publishes nothing at all. `agentic-hil adopt-hardware` writes it.
    serial_number: "066AFF495451885087171450"
    vid: 1155                   # the type, which is what makes the serial mean a unit
    pid: 14155
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
    alternatives = node.get("oneOf") if isinstance(node.get("oneOf"), list) else None
    if isinstance(declared, list):
        label = " or ".join(str(item) for item in declared)
    elif isinstance(declared, str):
        label = declared
    elif alternatives:
        # A key written two ways, a CAN identifier as an integer or as a
        # hexadecimal string, declares no `type` of its own. Reading it as
        # "any" would publish the one field shape a caller cannot guess as the
        # one field shape nobody constrained.
        label = " or ".join(_schema_type_label(member) for member in alternatives if isinstance(member, dict))
    else:
        label = "any"
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        label += ", one of " + ", ".join(f"`{json.dumps(item)}`" for item in enum)
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
        default = f"`{json.dumps(field['default'])}`" if "default" in field else ("**required**" if name in required else "-")
        description = str(field.get("description", "")).replace("\n", " ").replace("|", "\\|")
        if field.get("deprecated") is True:
            description = "**Deprecated.** " + description
        # The value shape needs the same escape the description gets, and for the
        # same reason: a `pattern` with an alternation in it (a CAN identifier
        # written decimal-or-hexadecimal, `flash_address`) carries a pipe, and a
        # pipe splits the row it is written in whether or not it sits in backticks.
        shape = _schema_type_label(field).replace("|", "\\|")
        rows.append(f"| `{name}` | {shape} | {default} | {description} |")
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
        heading = f"### `{name}`" + (" (required)" if name in required else "")
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

`{CONFIG_SCHEMA_URI}` serves the JSON Schema this file is validated against. A schema says what is **valid**; it does not say what a section is for, which ones a bench actually needs, or what a real one looks like filled in. This document is that, and it takes every value shape from the same schema at read time: there is no second list of types anywhere.

## Where the file is

One authoritative file per project, **outside** the workspace, so repository content can never rewrite policy. The server discovers it; `AGENTIC_HIL_CONFIG` may override the location with an absolute path. `{PLATFORM_PATHS_URI}` has the location rules.

`workspace_root` and `state_root` are the only two required keys. Everything else has a default, and a configuration that names only those two is valid: it just describes a bench with no hardware on it.

## The sections

{sections}

## A worked example

A Nucleo-F446RE on ST-Link, flashed through OpenOCD, talking over the probe's own virtual COM port. It was generated with every permission true but the two that refuse flashing while they are true; what you see beyond those is what the operator asked an agent to take back afterwards (no regeneration of this file from hardware discovery):

```yaml
{CONFIG_WORKED_EXAMPLE}```

`probe_id` is the value nobody can guess and nobody enjoys transcribing: `debugger_probes_list` reads it off the attached probe, and `project_config_set` enters it.

## Changing it over MCP

These calls are the only door. The file itself is protected by deny rules `agentic-hil setup` writes into the host, so an agent's own file tools cannot touch it: that is the precondition for this door, not a contradiction of it.

| Call | Does |
|---|---|
| `project_config_describe` | answers, for **this** configuration in **this** state, which keys you may change right now, which you may not, and which grant would open a locked one. Needs no permission; reading is free. |
| `project_config_set` | sets named keys, field-wise, after checking each value against the schema. |
| `project_config_adopt_hardware` | reads the attached probe and fills in the identity keys that are still unset, through `project_config_set`. Supplies no value of its own. |
| `project_config_create` | regenerates the whole file from hardware discovery when `permissions.allow_config_write` is set. It authors no permission value of its own, but it is a generation and not a narrowing: see below. |
| `project_config_reload_description` | makes a changed **description** the one this running server answers out of, without a restart. Re-reads nothing else. See "Picking up a changed description without a restart". |

### Permissions move one way: through `project_config_set`

A configuration is **generated with every permission true** (flashing, reset, COM and CAN writes, and all three `permissions.allow_config_*` grants) **except `allow_raw_debugger_commands` and `allow_mass_erase`, which are generated false**. Both of those act on flash outside the path this server validates (a raw debugger command writes whatever it is given, a mass erase clears whatever a flash has just written), so once either is allowed, a flash report's claim about what is on the device is no longer one this server can stand behind. That is the mutual exclusion between validated flashing and unrestricted debugger access: while either of those is true, `flash_firmware` on that probe is refused. Neither has a tool behind it here, so setting one true withholds flashing rather than granting anything, and leaving them false costs nothing and is what makes the bench flashable. The bench is workable from the moment the file exists, flashing included, and nobody has to open an editor to make it so.

What holds instead of a closed start is the direction of the one call that writes a permission field-wise:

```text
Generation                  every permission true, but the two flashing is interlocked against
Through project_config_set  write false into a permission
                            never true, not even into one you set to false yourself
Last move there             permissions.{CONFIG_PERMISSIONS_RIGHT}: false
After that                  no permission changes through project_config_set at all
Reopened by a person        `{CONFIG_GRANT_COMMAND} <key>`: one named permission, nothing else in the file
```

A change that would turn any permission on is refused as `{CONFIG_WIDENING_ERROR}`, and the check reads the permissions present in the document before and after the write rather than the keys the request named, so there is no spelling of it that gets through. Report the refusal and stop; granting is the operator's.

Closing `permissions.{CONFIG_PERMISSIONS_RIGHT}` freezes the permissions for this call: after it, no permission here can be changed again through `project_config_set`. That call says so in its own result: which permissions stand frozen, that you cannot undo it, and the commands a person reopens it with. Do not make that call in passing.

### What the ratchet does not cover

The two commands a person has are a separate door, and it is honest to say so rather than to promise more than holds:

* `{CONFIG_GRANT_COMMAND} <key>` at a person's shell opens one named permission in the file as it stands and changes nothing else in it; `{CONFIG_REVOKE_COMMAND} <key>` closes one again. That is the surgical reopen path, it is the operator's, and it is the one to name when a permission is what is missing, with the key. Neither is an MCP tool.
* `{CONFIG_REOPEN_COMMAND}` at the same shell rewrites this whole file from attached hardware at the generated defaults. That is the wide reopen path and it is also the operator's; ask for it only when the bench itself has to be rebuilt.
* `project_config_create` over MCP is the same generation under `permissions.allow_config_write`. Entries already in the file keep the permissions this server loaded for them; an entry the discovery finds for the first time arrives at the generated defaults; an entry the discovery no longer finds is dropped; and if the configuration has been deleted in the meantime, the file that comes back is at those same defaults. Its result names what it wrote.
* **"This server loaded" is literal, and it is the sharpest edge of that door.** A server parses the configuration once, at startup, and does not reload. A permission narrowed with `project_config_set` is on disk and is *not* in what the server holds, so a `project_config_create` in that same session writes the older, wider value back. A narrowing binds this path only once the server has been restarted onto the narrowed file, the same restart a `config_stale` result asks for, and that includes closing `permissions.allow_config_write` itself, which is checked against the loaded configuration like every other permission this server enforces. What closes the door for good is an operator setting it false in the file and the server being restarted onto it.
* Anything a person does at the command line, including editing the file, is theirs. Nothing here binds them.

So the claim is exactly this and no larger: **the MCP permission-write path can only narrow.** An operator who wants the whole file to stop moving from the agent side sets `permissions.allow_config_write` to false as well. That closes the regeneration door, and like every other permission here it can be closed from this surface and not reopened from it, taking effect for a running server once it is restarted onto the closed file.

### The two rights

| Grant | Opens |
|---|---|
{rights}

The split is the point. Somebody who opens the file so an agent can enter a probe serial has not, in the same motion and without being told, handed over that agent's ability to narrow the bench underneath somebody else's work.

### Which keys, and what may go in them

| Key | Grant that opens it | Value shape (from the schema) |
|---|---|---|
{keys}

`<name>` is an entry name you choose. A `debuggers`/`com_ports`/`can_buses` entry that does not exist yet is created by setting a key under it, always with every permission false, written by the server. A generation grants; a write only ever takes away, and adding an entry is a write, so a device named this way arrives closed and `{CONFIG_GRANT_COMMAND} <key>` at a person's shell is what opens it, one permission at a time.

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
| sending a whole document, or a whole `debuggers.dut` subtree | the agent does not author this file. `value` accepts a string, number, boolean or null and nothing else, so no subtree (and no `permissions:` block inside one) can arrive as a value. |
| turning any permission on through `project_config_set`, with any grant | refused as `{CONFIG_WIDENING_ERROR}`, both for a value other than `false` and for a document that would end up granting more than it did. The permissions actually present in the document are compared before and after the change, so it holds whatever the key was called and whichever grant the caller holds. Regenerating the file is the other door and a different grant. See "What the ratchet does not cover". |
| reaching a permission with only the description grant | refused twice over: the key does not resolve to the description grant, and the same before/after comparison catches a permission that moved without `{CONFIG_PERMISSIONS_RIGHT}`. |
| changing anything while a run is open | a run holds devices under the policy this file states. Changing the policy underneath it is refused; close the run with `bench_run_stop` first. |
| deleting a key, an entry, or the file | nothing on this surface removes configuration. Regenerating one costs an operator their settings. |
| `version`, `workspace_root`, `state_root`, `validation`, `recovery`, and every key of `artifacts` and `debug` except the two grants below | not settable over MCP at all. These decide where trusted state lives, which files may be flashed and how far the machine may recover itself. They are an operator's to write. |
| widening `artifacts.allow_upload` or `debug.allow_all_symbols` | those two are the exception to the row above: a generation grants them like every other permission, so `{CONFIG_PERMISSIONS_RIGHT}` reaches them and `false` is the only value that may be written into either. Their list and path neighbours (`artifacts.allowed_roots`, `debug.allowed_symbols`, `upload_directory`) stay operator-only, so narrowing here is the scalar grant and nothing else. |

A refused write names the grant that is missing and the key in this file that carries it. If the answer is `permission_denied`, that **is** the answer: report it and stop. The configuration belongs to the operator.

## A board plugged in after this file was written

`agentic-hil setup` discovers hardware once. Run with nothing attached, it writes placeholders (`probe_id: null`, `executable: null`, `controller: "unknown-controller"`, no `com_ports` entry), and that is the common case, because installing the tool and connecting the board are two separate moments.

`project_config_adopt_hardware` is the way back in. It reads what is attached and fills in what the file has nothing for.

```json
{{"name": "project_config_adopt_hardware", "arguments": {{"apply": true}}}}
```

| Property | Rule |
|---|---|
| what it carries | `debuggers.<name>.probe_id`, `debuggers.<name>.executable`, `target.controller`, `com_ports.<name>.device`. Identity, and only identity: what an attached probe hands you. |
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

A server parses this file once and enforces that document until it exits. That rule is about the **permissions**: they were taken as a whole, and a document that turned up underneath a running server may not widen them. It used to hold the **description** hostage too: a board plugged in and written down after the server started was invisible to it, and the only way across was a restart of the agent's MCP server.

`project_config_reload_description` is the way across. It takes no arguments.

```json
{{"name": "project_config_reload_description", "arguments": {{}}}}
```

| Property | Rule |
|---|---|
| what it re-reads | `target`, `debuggers`, `com_ports`, `can_buses` (minus each entry's `permissions:` block). That is the whole list. |
| what it does not | **every permission, in either direction.** Not narrowed, not compared, not adopted. The grants in force after a reload are byte for byte the ones parsed at startup. |
| a device that is new to this server | arrives with **no grant at all**. It can be probed and read (from `version: 2` on, reading needs no grant; exclusivity is what protects it), and flashing, reset, mass erase and COM/CAN writes are denied on it until an operator restarts the server onto the file that grants them. |
| a device renamed on disk | the new name is a device this server has never seen, so it arrives closed, and the old name is gone. Renaming an entry costs its grants until a restart. |
| which board is bound | the name this server already drives, wherever it still exists. A second entry appearing does not repoint a bound server; a bench that configured no debugger at all binds the first one that appears, because there is exactly one board it could mean. |
| refused while | a run, a COM/CAN session or a debug session holds this bench (`config_reload_in_open_run`), or an incident on it is unresolved (`resource_quarantined`). Both because a held name has to keep meaning the same physical board. |
| refused when | the file is missing, unreadable, or does not load: `config_file_not_found`, `config_unreadable`, `config_invalid`, the same three states `config_status` reports. Nothing changes on any of them. |
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

At a shell the same operation is `agentic-hil config-reload`, which loads this file the way a server does and reports what a running server's reload would take from it and what it would leave: the pre-flight for asking an agent to make the call.

## Getting from "board attached" to a valid change

1. `project_config_describe`: what may this caller change right now.
2. `project_config_adopt_hardware`: what is attached, and which keys it would fill in. Nothing is written yet.
3. The same call with `{{"apply": true}}`, or `project_config_set` for a key it left alone.
4. `project_config_reload_description`: make what was just written the description this server answers out of. The write's `reload_required` is about that, and this is what clears it for the four device sections. A permission the write changed still waits for a restart.
"""


LEASE_LIFECYCLE_DOCUMENT = """# Device exclusivity, hardware leases, and the quarantine lifecycle

Two different things guard the hardware, and they live in two different places.

**Exclusivity** is machine-wide and keyed on the physical device. It is what replaced the read permission: reading needs no grant, because whoever holds a board cannot be disturbed and whoever reads while no run holds it disturbs nobody.

**The lease** is per configuration, lives under `state_root`, and records what a call did and whether the hardware was left in a confirmed state. It survives process exit; that is what makes an abandoned incident visible instead of forgotten.

A third thing *describes* the hardware contact and guards nothing. Every tool in `tools/list` carries MCP `annotations` (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) set from what the tool demonstrably does, on the same line this document already draws for hardware contact. `project_config_reload_description`, `com_read`, `can_read`, `bench_run_status` and `project_config_describe` change nothing and say so; `flash_firmware` and `debug_start_session` (whose `load` mode programs flash, which is why it needs `allow_flash`) are declared destructive; `probe_target` is deliberately *not* read-only, because an SWD attach halts the core. They exist so a host can tell a harmless call from an irreversible one instead of judging by the tool's name. They are hints in the protocol's sense and this server enforces nothing by them: what a call may do is decided by the configuration's permissions and by the exclusivity below, exactly as before. A `readOnlyHint: true` is not a grant, and a `destructiveHint: false` is not a promise that a call will be allowed.

## Device exclusivity

| Property | Rule |
|---|---|
| what is locked | the physical device: `physical:<resource_id>`, `probe:<serial>`, `probe-exe:<executable>`, `com:serial:<serial_number>`, `com:<device>`, `can:<adapter>:<channel>` |
| case | a name for hardware (`resource_id`, a probe serial, a port's `serial_number`, a CAN channel) folds case on every platform, because `0669FF` and `0669ff` are one unit wherever the bench runs. A host path (a debugger executable, a serial device) folds the way its own filesystem does, so `COM7` and `com7` are one port on Windows while `/dev/ttyACM0` and `/dev/ttyacm0` are two on Linux. Two entries whose `resource_id` values differ only in case are refused at config load rather than merged |
| a serial port's identity | `com_ports.<name>.serial_number` is the adapter's USB serial and is what the lock follows; `device` is only how the port is opened. Without it the key falls back to the device name, which is an enumeration order (attaching a second adapter can hand one entry another board), so an entry that names neither a `serial_number` nor a `resource_id` carries an `identity_warning` saying so, and from `version: 3` on that warning is a property of the file instead: such an entry must declare what identifies it with `identity_source` or the configuration is refused at load, naming `agentic-hil adopt-hardware`. `vid`/`pid` sit beside the serial and name the device *type* rather than a unit, so they are never a lock key: a USB serial is unique only within its vendor, so a serial matching under a foreign vid/pid is refused too, and an adapter that publishes no serial at all is compared on the type alone, which separates a CH340 from an ST-Link and not one CH340 from another, and `identity_source: vid_pid` says exactly that. An entry that *does* name hardware is opened on one ground only: the attached device is `confirmed` to be the board it names. A port that has come to be a different board is refused with `com_port_identity_mismatch`; a port whose identity cannot be checked at all (no serial backend, not enumerated exactly once, no serial reported, or no USB ids reported where the entry names them) is refused with `com_port_identity_unverified`, because a check that could not run does not prove the name still leads to its board. Both refuse before the port is opened and are retry-safe. An entry that names no hardware is opened as `not_declared`, unverified by design |
| where | `~/.agentic-hil/device-locks`, one agreed place per machine, never under `state_root`: a lock kept per configuration is not a bench lock |
| how long | the whole run, from the declaration to its end; the lease each call takes borrows that hold |
| what may be touched | only what the test description declares; anything else is refused with `undeclared_device` |
| contention | refused with `device_busy`, naming the holder; waiting happens only when the caller asked for it with `wait_s`, and stays bounded |
| a crashed owner | the operating system drops the lock when the process dies, so the device is free immediately: no quarantine, no `recover`, no waiting |

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

A written test plan needs no declaration around it. `test_reactor_run` drives the same reactor `agentic-hil test-reactor` drives, and there the plan *is* the declaration: every device it names is taken before its first step and held past its last, and a step reaching for one the plan did not name is refused with `undeclared_device` exactly as a call inside a `bench_run_start` would be.

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

So an abandoned run costs nothing beyond the life of the server process. While it lasts, a contender's `device_busy` refusal carries `heartbeat_age_s` and, past four heartbeat intervals, `holder_heartbeat_stale: true`. An idle holder is visible rather than merely obstructive. Call `bench_run_status` if you are unsure whether you still hold the bench, and `bench_run_stop` to be sure you do not.

One case is outside this: a server process left running with its stdin never closed, by a host that leaked the pipe. It sees no disconnect and holds the run. Ending that process is the answer; never delete a lock file under `~/.agentic-hil/device-locks`.

### Two config entries, one board

The lock is keyed on the hardware, not on the name of the config entry. Two entries that describe one physical unit (a debug probe and its virtual COM port sharing a `resource_id`, or the same serial device configured twice) resolve to one lock key and are taken once. Declaring both is not an error and does not double-lock anything.

The one identity that is *not* hardware-derived: a debugger entry with neither `resource_id` nor `probe_id` falls back to the backend toolchain. Two boards driven by the same backend would then share one lock, and one board reached through two backends would take two. `bench_run_start` returns a `warnings` entry when a declared device is in that state; the fix is a `probe_id`, or a `resource_id` shared by every entry naming that unit.

Reading can still perturb a target: an SWD attach halts the core, a CAN controller outside `listen_only` sends dominant ACK bits, opening a serial port raises DTR on boards that wire it to reset. That is why the passive modes stay available: `can_buses.<name>.listen_only: true` and `com_ports.<name>.assert_dtr: false` / `assert_rts: false` are how a target is observed provably undisturbed. They are no longer a precondition for access; they are the way to prove a reading did not touch anything.

What `listen_only: true` is worth is what the adapter can be held to, and that differs by adapter, so it is enforced rather than assumed.

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

An incident answers one question, "is the physical state of the hardware unknown?", never the weaker "did something go wrong?". A failure that can prove it never reached the hardware refuses with a named error and `retry_safe: true`, leaves no incident record, and keeps the bench in service; the board is exactly as the last call that did reach it left it.

A *standing* quarantine answers a narrower one still: "is the missing proof one that cannot come back on its own?". Only a broken audit trail qualifies, because no reset writes a report that was never written. A target's state comes back with the next reset into halt and an answering probe; a serial handle's and a CAN adapter's with their own next open, which the operating system refuses by itself if the handle is really stuck. So the peripheral cleanup reasons record their event and open no incident at all, and every other incident is open only for the length of the call that raised it: the recovery action runs, and whatever it did not settle stands down at the end of the call with a `no_standing_state` line in the recovery ledger. Nothing is lost from the record; what goes is the hold.

| Failure | Outcome |
|---|---|
| toolchain executable not found (OpenOCD, pyOCD, STM32CubeProgrammer CLI, the debug server, GDB) | refusal: no process ever existed |
| OpenOCD rejected the command before `init`, or a `-f` script failed to load, or the adapter could not be opened, or no target answered, with the init-stage marker absent | refusal: `init` never completed, so nothing was brought under debug control |
| pyOCD found no probe / could not open it, refused the configured `target_type`, or reported its connect sequence failed | refusal: no core came under debug control |
| ST-Link probe absent (`no ST-LINK detected`) or no target behind it (`No STM32 target found`) | refusal: the channel carried nothing |
| `probe_target` / `debugger_probes_list` failed and the backend named no abort point (a timeout that killed it mid-call (`timeout`), an exit whose output does not carry the confirmation the tool asks for (`target_state_unconfirmed`)) | **incident** (`debugger_readonly_target_state_unconfirmed`): being read-only is not being passive, an SWD attach halts the core, and a killed process never ran its own `shutdown`. Settled by a verified reset-into-halt, never by a re-read. The `error_type` is its own, never `target_not_detected`: that one is the backend's report that the adapter was reached and nothing answered, which is a row above and refuses |
| COM port could not be opened and the handle is verifiably closed | refusal: the port never carried a byte of the session |
| CAN adapter never initialized on SocketCAN (python-can `CanInitializationError`) | refusal: `SocketcanBus()` only creates and binds a socket; the controller is brought up out of band |
| CAN adapter never initialized on PCAN (`PcanCanInitializationError`) | **recorded event** (`can_open_cleanup_unconfirmed`): python-can raises it from four `SetValue` calls that run after `PCANBasic.Initialize` succeeded, and the class carries no phase marker, so an initialized channel that is already ACKing on the bus looks the same. The refusal names it and carries its guidance; the bus is not held for it, because the next `can_session_start` is what settles an adapter either way |
| a direct CAN read failed | refusal: `recv()` transmits nothing |
| a peripheral cleanup that did not confirm (a serial handle or reader that would not close, a CAN adapter that would not shut down) | recorded event with the same reason and the same guidance, and no incident: the next open is the proof, and a handle that is really stuck makes it fail through the operating system |
| anything else whose abort point cannot be proven (an unconfirmed flash or reset, an exception mid-call, an owner that died holding a lease) | incident for the length of the call: the recovery action runs, and what it does not settle stands down rather than standing |
| a broken audit trail (`*_audit_broken`) | **standing quarantine**; that is the feature, and it is the only one left. No reset writes a report that was never written |

Between the two sits the self-resolving class: a reason the next hardware call can settle itself under `recovery.auto_recover` (a read-only re-read, or a verified reset-into-halt) is cleared by the machine, without an operator, and attested in the ledger as `recovery_action_verified`.

## What a justified quarantine tells the signer

`recover --confirm-safe-state` is a signature, so every result naming a `cleanup_reason`, and `hardware_lease_status`, carry `quarantine_guidance`: one entry per reason with `attempted` (what was being done when confirmation was lost), `confirmed` (what still holds), `unknown` (the exact gap that makes a machine answer impossible), and `physical_check` (what to verify on the physical board before signing). An incident recorded by another version gets an explicit fallback entry rather than silence. Guidance follows the reason and not the gate: what a call could not confirm is worth reading whether or not anything is being held for it.

## When a call is refused with `resource_quarantined`

This is the audit halt, and since it narrowed to that it is the only refusal of its kind: the evidence chain for this bench could not be written or read, and no hardware action rebuilds it.

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

The recorded config is what defined the resources, permissions, and limits under which the incident happened; recovering against a different one would clear an incident nobody assessed. The operator reviews the delta between the two configs and reruns with `--accept-config-change` to state explicitly that the change is understood and accepted. The `hardware_recover` MCP tool takes the same override as `accept_config_change: true`, for the agent that has just shown the operator both digests and been told to go ahead; the refusal names both spellings. Either way the override is recorded in the recovery audit log as `config_change_accepted`, beside `recorded_config_sha256` and `current_config_sha256`.

## `recovery.auto_recover` policy

Set in the authoritative configuration; decides how far the owning process may go on its own.

| Value | Owner may | Use when |
|---|---|---|
| `off` | nothing; only an operator clears a quarantine | any bench where a machine decision is unacceptable |
| `readonly` | reap this owner's debugger processes and re-read the probe; nothing physical | peripherals react to a reset |
| `reset_halt` (default) | additionally drive a reset-into-halt and read it back, which settles every reason except the audit-broken families | default bench |

`reset_halt` degrades to `readonly` when the bound debugger lacks `allow_reset`. `recovery.max_attempts` (default 3, range 1-10) caps machine attempts per incident before the owner defers to an operator.

## Recovery outcomes

| error_type | Meaning |
|---|---|
| `operator_confirmation_required` | `--confirm-safe-state` was not given |
| `quarantine_id_required` | `--quarantine-id` was not given |
| `resource_not_quarantined` | nothing to recover; a bench whose incident does not stand answers `ok: true` with `nothing_to_recover: true` instead, because nothing went wrong |
| `quarantine_changed` | the incident or a resource marker moved; re-read `lease-status` |
| `config_changed` | see `--accept-config-change` above; over MCP, `accept_config_change: true` |
| `resource_busy` | a live owner still holds project resources; stop it first |
| `recovery_audit_failed` | the audit could not be persisted, so the quarantine stands |
"""


PLATFORM_PATHS_DOCUMENT = """# Where each file lives, and which command touches it

Agentic HIL keeps three things outside the workspace: the authoritative configuration, `state_root`, and the machine-wide device locks. This is where each of them goes on each platform, and how to move the first two when the default location does not suit the machine.

## What is checked about a path, and what is not

A configured path is opened component by component without following links, and every component of the chain is held open while the operation runs (on Windows without `FILE_SHARE_DELETE`, which blocks a rename or a delete of any of them for the duration). So a path is refused when it *is not what it claims to be*: a symlinked component, a file where a directory is needed, a final object that is not a single-link regular file. That refusal carries `error_type: unsafe_configured_path` and names the component that stopped the walk.

What is **not** asked is who else on this machine could write the path. A Windows ACL walk and a POSIX mode/sticky-bit walk over every ancestor used to ask exactly that, and both were removed in 0.8.0. Two reasons, and the second is the one that decides it:

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

The executable that check accepts is one owned by you or by root, executable, and writable by nobody else, and it reads group write as another writer only when the group is not your own user-private group (its name is your account's, its gid is your primary gid, and it has no other member), so the group-writable console script a default Debian or Ubuntu `umask 0002` produces registers as it stands, while world write, a foreign group, and ownership by another account are refused with the failing condition and the path element it failed on named under `rejected_candidates` in `agentic-hil doctor`.

## Where things go

| Item | Windows | POSIX |
|---|---|---|
| authoritative configuration | `%APPDATA%\\agentic-hil\\projects\\<name>-<digest>\\config.yaml`, else `%USERPROFILE%\\.agentic-hil\\projects\\<name>-<digest>\\config.yaml` | `$XDG_CONFIG_HOME/agentic-hil/projects/<name>-<digest>/config.yaml`, else `~/.agentic-hil/projects/<name>-<digest>/config.yaml` |
| `state_root` | `%LOCALAPPDATA%\\agentic-hil`, else `%USERPROFILE%\\.agentic-hil\\state` | `$XDG_STATE_HOME/agentic-hil`, else `~/.agentic-hil/state` |
| record of configurations bound by `AGENTIC_HIL_CONFIG` | `%APPDATA%\\agentic-hil\\external-projects.json`, and `%USERPROFILE%\\.agentic-hil\\external-projects.json` | `$XDG_CONFIG_HOME/agentic-hil/external-projects.json`, and `~/.agentic-hil/external-projects.json` |
| device locks | `%USERPROFILE%\\.agentic-hil\\device-locks`, fixed | `~/.agentic-hil/device-locks`, fixed |

Two roots wherever a row names two, best first: the platform default, then `~/.agentic-hil`, which is the walk `agentic-hil init` runs and the root every path refusal already recommends. `else` is meant literally, the second root is taken when the first cannot be written, checked as a write rather than as merely existing, which is what a redirected profile or a packaged host does to the default. The configuration adds one rule on top of that order, that an existing file wins over it: a configuration already written under the fallback is this workspace's authoritative one, every later load finds it there, and `init --force` rewrites it where it is rather than generating a second beside the default. The record is the row spelled `and`, because both of its files can hold entries at once; the `AGENTIC_HIL_CONFIG` bullet below says how they are read and which one a write lands in.

The device lock directory is not configurable and has no environment override. It is the one place every process on this machine agrees to look for who holds a board, and an override is how two sessions stop seeing each other, which is the failure it exists to prevent. The home directory is chosen because every process of one user reaches it without having to agree on a configuration first.

## Choosing another location

`AGENTIC_HIL_CONFIG` and a freely chosen `state_root` are not debug switches. They are how a project binds to a configuration and a state directory the discovered defaults do not cover: a redirected profile directory, a roaming share, a volume the operator would rather keep this off.

```text
AGENTIC_HIL_CONFIG=C:\\Users\\<user>\\.agentic-hil\\projects\\<name>-<digest>\\config.yaml

# inside that config.yaml
state_root: C:\\Users\\<user>\\.agentic-hil\\state
```

Using them costs nothing else: `state_root` has no fixed location beyond being absolute and not overlapping `workspace_root`, and a configuration selected by `AGENTIC_HIL_CONFIG` is read exactly like a discovered one: same schema, same validation, same permissions. Nothing about a project is second class for having taken this route.

The one rule that does not bend: set `AGENTIC_HIL_CONFIG` in the host's user-level, managed, or parent-process environment, **never** in a repository-controlled file (`.vscode/mcp.json`, `.mcp.json`, `.codex/config.toml`, `opencode.json`). An agent that can edit the file that selects the configuration can select a configuration it wrote.

Rules that hold on both platforms:

- `AGENTIC_HIL_CONFIG` is optional and must be an absolute path to the configuration file.
- `agentic-hil init --agent <agent>` writes the location of a configuration bound this way into `external-projects.json` beside the projects directory, and it holds nothing but locations. Two spellings exist, one beside each projects root (the platform default and `~/.agentic-hil`), and both may hold a record at once: an unpackaged host leaves one beside the default, a virtualized profile writes beside the fallback. Readers union both, so no project goes missing whichever file holds it; a write lands in the one root the profile can actually write (checked as a write, not merely as existing) and converges the union into it, while an unwritable record is read but never targeted. `agentic-hil uninstall` removes what it can and reports a record it cannot remove under `left_alone` with the reason, rather than aborting after the deny rules are already taken back. The write refusals that run leaves in the agent's own settings are refreshed on every later run, and a project whose configuration the projects directory does not hold would otherwise be read there as a bench that is gone, so its rule would be taken back while the bench still wanted it. A recorded configuration that cannot be read leaves that question open and no rule is taken back at all; `agentic-hil uninstall` takes the record back together with the rules it explains.
- `workspace_root` and `state_root` are both mandatory and absolute, and must not overlap in either direction.
- The discovered default configuration path is derived from the workspace path, so it is canonical per workspace; a config found elsewhere is only accepted through `AGENTIC_HIL_CONFIG`.
- Whether an agent may write the configuration is decided by the configuration, in `permissions.allow_config_write`, and by nothing else. There is no second state store: what holds is what a person reads in the file. A workspace with no configuration lets an agent generate one, and a configuration deleted out of band lets it generate a fresh one: the generated skeleton again, at the generated defaults, so the round trip discards every narrowing the operator had asked for and produces the file `agentic-hil init` would have written.
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

The two OpenOCD values above are search names: OpenOCD resolves them against its own script path, so they name no file on this host and the configuration accepts them without one. Give an absolute path instead when this bench should run exactly the script files it names; a path is then checked as a path, and must exist, live outside the workspace, and not be under the system temporary directory.

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

That rule exists because of what happened without it: an agent looking for the right `target_type` found nothing about packs anywhere, escalated through pyOCD's installed sources and `cmsis_pack_manager`'s internals, and ended up fetching a `.pdsc` from a vendor site by hand (twice) without anyone confirming the download. Do not reconstruct a pack from hand-downloaded `.pdsc` files. `pyocd pack install` is the one supported route.

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
| `undetermined` | this host could not answer: no toolchain installed, the enumeration failed or could not be read | green, and `undetermined_reason` says why |
| `not_configured` | no `target_type` is set, so pyOCD would guess from the probe's board ID | green |
| `not_applicable` | this backend has no target type: OpenOCD uses `target_cfg`, STM32CubeProgrammer identifies the part itself | green |

The line between `unsupported` and `undetermined` is deliberate. `unsupported` is a fact about this host; `undetermined` says nothing about the configuration and must not be read as one. A bench with no debugger toolchain installed yet stays green: `agentic-hil setup` rolls back on a red `doctor`, so conflating the two would break installation on exactly the fresh machine the check is meant to help.

`undetermined` is also not a pass. It means the question was asked and went unanswered, and the `doctor` summary says so.

## A green run can depend on a pack nobody recorded

If `target_type` resolves on one machine and not on another, the difference is the installed pack, not the configuration. `pyocd pack show` reports what the working machine actually has.
"""


# Two plans that load. Constants rather than lines inside the document below,
# because the test that pins this reference parses them back out and puts them
# through the reactor's own schema validation and version gate: an example a
# reader cannot run is worse than none, and this is a document meant to be
# copied from.
PLAN_MINIMAL_EXAMPLE = r"""version: 5
name: boot-smoke
steps:
  - {device: dut, action: flash, image_path: build/app.elf, reset_after_flash: true}
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_expect, text: "boot complete", timeout_s: 10}
"""

PLAN_COMPARATOR_EXAMPLE = r"""version: 5
name: capture-in-range
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_write, text: "capture\n"}
  - {device: dut_uart, action: uart_read, comparator: {pattern: "temp=(\\d+)C", range: {min: 20, max: 30}}, timeout_s: 5}
  - {device: dut_can, action: can_open}
  - {device: dut_can, action: can_read, comparator: {id: "0x201", equals: "01 FF"}, timeout_s: 5}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: read_symbol, symbol: capture_count, size_bytes: 4, comparator: {range: {min: 1, max: 8}}}
"""


def _plan_names(names: object) -> str:
    spelled = [f"`{item}`" for item in names if isinstance(item, str)]
    if len(spelled) < 2:
        return "".join(spelled)
    return " and ".join([", ".join(spelled[:-1]), spelled[-1]])


def _plan_version_enum(schema: JsonObject) -> list[int]:
    node = _dereference(schema, (schema.get("properties") or {}).get("version"))
    return [item for item in node.get("enum", []) if isinstance(item, int)]


def _plan_step_entries(schema: JsonObject) -> list[tuple[str, JsonObject]]:
    """Every step the format admits, as (action, its schema entry), in schema order.

    Read out of the one list the validator reads, the `oneOf` under
    `steps.items`, so a step added to the format appears in this document
    without anybody writing it down a second time."""
    steps = _dereference(schema, (schema.get("$defs") or {}).get("steps"))
    items = _dereference(schema, steps.get("items"))
    entries: list[tuple[str, JsonObject]] = []
    for member in items.get("oneOf", []):
        node = _dereference(schema, member)
        action = _dereference(schema, (node.get("properties") or {}).get("action")).get("const")
        if isinstance(action, str):
            entries.append((action, node))
    return entries


def _plan_feature_version(schema: JsonObject, node: object) -> int | None:
    """Which plan version a node belongs to, read the way the version gate reads it.

    A marker written on the step or key itself wins over the definition a `$ref`
    points at, which is the precedence `reject_newer_features_in_steps` applies:
    `can_read`'s own `timeout_s` arrived in version 3 while the definition it
    borrows its shape from is as old as the format."""
    if not isinstance(node, dict):
        return None
    merged = {**_dereference(schema, node), **node}
    since = merged.get(PLAN_FEATURE_VERSION_KEY)
    return since if isinstance(since, int) else None


def _plan_exclusive_names(node: JsonObject) -> list[str]:
    """The keys a `oneOf` of the shape "this one, and not that one" names."""
    names: list[str] = []
    for member in node.get("oneOf", []):
        if not isinstance(member, dict) or "not" not in member:
            return []
        names += [str(item) for item in member.get("required", [])]
    return list(dict.fromkeys(names))


def _plan_choice_names(node: JsonObject) -> list[str]:
    """The keys an `anyOf` of bare `required` branches names: at least one of them."""
    names: list[str] = []
    for member in node.get("anyOf", []):
        if not isinstance(member, dict) or set(member) != {"required"}:
            return []
        names += [str(item) for item in member.get("required", [])]
    return list(dict.fromkeys(names))


def _plan_constraint_notes(schema: JsonObject, node: JsonObject) -> list[str]:
    """What a step or a comparator may not spell, read off the schema's own combinators.

    Every one of these is refused before the run starts, so a plan author reads
    them here rather than off a red bench."""
    notes: list[str] = []
    exclusive = _plan_exclusive_names(node)
    if exclusive:
        notes.append(f"Exactly one of {_plan_names(exclusive)}. Both, or neither, is refused.")
    choice = [name for name in _plan_choice_names(node) if name not in PLAN_ROUTE_KEYS]
    if choice:
        notes.append(f"At least one of {_plan_names(choice)}.")
    for key, needs in (node.get("dependentRequired") or {}).items():
        notes.append(f"`{key}:` is written only beside {_plan_names(needs)}.")
    for key, member in (node.get("dependentSchemas") or {}).items():
        if not isinstance(member, dict):
            continue
        if member.get("required"):
            narrowed = [
                f"`{name}` is then `{_schema_type_label(_dereference(schema, raw))}`"
                for name, raw in (member.get("properties") or {}).items()
            ]
            notes.append(f"`{key}:` requires {_plan_names(member['required'])}" + ("; " + ", ".join(narrowed) if narrowed else "") + ".")
        refused = member.get("not") if isinstance(member.get("not"), dict) else {}
        if refused.get("required"):
            notes.append(f"`{key}:` is refused beside {_plan_names(refused['required'])}.")
    return notes


def _plan_routing(node: JsonObject) -> str:
    """Which key this step names its device with, and whether it has to."""
    present = [name for name in (node.get("properties") or {}) if name in PLAN_ROUTE_KEYS]
    if not present:
        return "nothing: the reactor runs this step itself"
    spelled = " or ".join(f"`{name}:`" for name in present)
    required = [name for name in _plan_choice_names(node) if name in PLAN_ROUTE_KEYS]
    return spelled if required else spelled + ", which a plan may omit while the project configures exactly one probe"


def _plan_version_rows(schema: JsonObject) -> list[str]:
    """What each format version added, taken from the markers the version gate reads."""
    steps_by_version: dict[int, list[str]] = {}
    keys_by_version: dict[int, dict[str, list[str]]] = {}
    for action, node in _plan_step_entries(schema):
        since = _plan_feature_version(schema, node)
        if since is not None:
            steps_by_version.setdefault(since, []).append(action)
        for name, raw in (node.get("properties") or {}).items():
            key_since = _plan_feature_version(schema, raw)
            if key_since is not None:
                keys_by_version.setdefault(key_since, {}).setdefault(name, []).append(action)
    rows: list[str] = []
    for version in _plan_version_enum(schema):
        steps = steps_by_version.get(version, [])
        added = [f"the {_plan_names(steps)} step{'s' if len(steps) > 1 else ''}"] if steps else []
        for name, actions in keys_by_version.get(version, {}).items():
            # A key that arrived on nothing but the steps this same version
            # introduced is not a second entry: the step already says when it
            # became writable.
            if set(actions) <= set(steps):
                continue
            where = _plan_names(actions) if len(actions) < 4 else f"all {len(actions)} steps that name a device"
            added.append(f"`{name}:` on {where}")
        rows.append(f"| `{version}` | {'; '.join(added) or 'the baseline: every step and key this table does not mark as newer'} |")
    return rows


def _plan_step_index_rows(schema: JsonObject) -> list[str]:
    baseline = min(_plan_version_enum(schema), default=2)
    return [
        f"| `{action}` | `{_plan_feature_version(schema, node) or baseline}` | {_plan_routing(node)} |"
        for action, node in _plan_step_entries(schema)
    ]


def _plan_merged_node(schema: JsonObject, raw: object) -> JsonObject:
    """One schema node with its `$ref` folded in and what is written locally on top.

    The precedence the reactor's own `resolve_schema_node` applies. Following
    the reference alone would drop exactly the keys this format writes beside
    one: `can_read`'s `wait_timeout_s` borrows the shape of a timeout and says
    for itself what it is a timeout on."""
    if not isinstance(raw, dict):
        return {}
    merged = {**_dereference(schema, raw), **raw}
    # Dropped, so a second dereference downstream does not resolve back to the
    # bare definition and undo the merge.
    merged.pop("$ref", None)
    return merged


def _plan_field_table(schema: JsonObject, node: JsonObject, skip: set[str]) -> list[str]:
    """The value shapes of one step or comparator, minus the keys said elsewhere."""
    properties = {name: _plan_merged_node(schema, raw) for name, raw in (node.get("properties") or {}).items() if name not in skip}
    rows = _schema_field_rows(schema, {**node, "properties": properties})
    return ["", "| Key | Value shape | Default | Meaning |", "|---|---|---|---|", *rows] if rows else []


def _plan_step_documents(schema: JsonObject) -> list[str]:
    baseline = min(_plan_version_enum(schema), default=2)
    blocks: list[str] = []
    for action, node in _plan_step_entries(schema):
        since = _plan_feature_version(schema, node) or baseline
        lines = [f"### `{action}`", "", f"Version {since} on. Routes with {_plan_routing(node)}."]
        description = str(node.get("description", "")).replace("\n", " ")
        if description:
            lines += ["", description]
        notes = _plan_constraint_notes(schema, node)
        if notes:
            lines += ["", *[f"* {note}" for note in notes]]
        lines += _plan_field_table(schema, node, {"action", *PLAN_ROUTE_KEYS})
        blocks.append("\n".join(lines))
    return blocks


def _plan_comparator_documents(schema: JsonObject) -> list[str]:
    """One block per comparator family, keyed on the definition each step points at.

    Which family a step carries is read from that step's own `comparator`
    reference, so a family added for a new medium documents itself against the
    steps that actually use it."""
    users: dict[str, list[str]] = {}
    for action, node in _plan_step_entries(schema):
        raw = (node.get("properties") or {}).get("comparator")
        reference = raw.get("$ref") if isinstance(raw, dict) else None
        if isinstance(reference, str):
            users.setdefault(reference.rsplit("/", 1)[-1], []).append(action)
    blocks: list[str] = []
    for name, actions in users.items():
        node = _dereference(schema, (schema.get("$defs") or {}).get(name))
        since = _plan_feature_version(schema, node) or min(_plan_version_enum(schema), default=2)
        lines = [f"### The comparator on {_plan_names(actions)}", "", f"Version {since} on."]
        description = str(node.get("description", "")).replace("\n", " ")
        if description:
            lines += ["", description]
        notes = _plan_constraint_notes(schema, node)
        if notes:
            lines += ["", *[f"* {note}" for note in notes]]
        lines += _plan_field_table(schema, node, set())
        blocks.append("\n".join(lines))
    return blocks


def plan_format_document() -> str:
    """What a plan author needs, generated from the schema the reactor validates against.

    Every step, key, default, version marker and combinator rule below is read
    out of that schema when this is called. What is written here by hand is what
    a schema cannot carry: where the file goes, how its path resolves, which
    refusal each mistake earns, and two plans that run.

    Not named ``test_plan_document`` for the reason ``plan_schema_document``
    is not."""
    schema = plan_schema_document()
    versions = _plan_version_enum(schema)
    steps_node = _dereference(schema, (schema.get("$defs") or {}).get("steps"))
    return f"""# How to write an Agentic HIL test plan

`{TEST_PLAN_SCHEMA_URI}` serves the JSON Schema every plan is validated against. A schema says what is **valid**; it does not say where the file goes, how its path resolves, which version admits which step, or what a comparator claims. This document is that, and it reads every step, key, default and version marker out of that same schema. There is no second list of the format anywhere.

A plan is run with the `test_reactor_run` tool, or by an operator with `agentic-hil test-reactor`. Both drive one reactor: the same preflight before the first hardware action, the same devices locked for the whole plan, the same permission judged per step, the same report.

## Where the plan is, and how its path resolves

| | |
|---|---|
| Default | `{DEFAULT_TEST_CONFIG_PATH}`, relative to `workspace_root` |
| Named instead | `test_config_path` on `test_reactor_run`, `--test-config` on the command line |
| A relative path | resolved against `workspace_root`, never against the process's working directory |
| An absolute path | taken as written |
| Either way | it has to resolve **inside** `workspace_root`, symlinks followed |
| Format | YAML or JSON, one mapping at the root, duplicate keys refused |

`workspace_root` is the authoritative configuration's binding of this project, and it is what makes a plan path mean the same thing to the tool, to the command line and to a detached worker. It is not something a plan can move: `{CONFIG_SHAPE_URI}` has where that file lives and how it changes.

Every mistake here is a refusal before the first hardware action, and each has its own `error_type` (`{ERRORS_URI}` has the fixes):

| What is wrong | `error_type` |
|---|---|
| The path resolves outside `workspace_root` | `test_config_invalid` |
| Nothing is at that path | `test_config_not_found` |
| It is there and will not open | `test_config_unreadable` |
| It is not valid YAML or JSON, or its root is not a mapping | `test_config_invalid` |
| It does not match the schema | `test_config_invalid`, with the field path of the step |
| It uses a step or key newer than its own `version:` | `test_config_invalid`, naming the version that introduced it |

## The document

Two keys are required: `version:` and `steps:`. `name:` is optional and defaults to the file's own stem; it is what the report calls the run. Nothing else may be written at the root. `steps:` holds {steps_node.get("minItems", 1)} to {steps_node.get("maxItems", 128)} steps, executed in order and stopped at the first one that fails.

## The versions

`version:` is one of {", ".join(f"`{version}`" for version in versions)}. A plan is held to what its own version contains, so a plan reaching for a newer step or key is refused by name rather than running here and failing on an older install for no stated reason. Nothing is removed by a later version: a plan written against an older one keeps loading and behaving exactly as it did.

| Version | What it adds |
|---|---|
{chr(10).join(_plan_version_rows(schema))}

## The steps

Every step names its `action:` and the configured entry it drives. From version 3 on that entry is named with one key, `device:`, and the authoritative configuration is what knows whether the name belongs to its `debuggers`, `com_ports` or `can_buses` section. The version 2 keys `debugger:`, `port_id:` and `bus_id:` stay valid as readable aliases; a step writes one or the other, never both.

A step naming a device the configuration does not declare, or one the run did not lock, is refused before anything is touched. So is a step whose permission the device's entry does not grant: `{ERRORS_URI}/permission_denied` has what to do with that, and the answer is never to edit the configuration.

| Step | Since | Routes with |
|---|---|---|
{chr(10).join(_plan_step_index_rows(schema))}

{chr(10).join(chr(10).join(("", block)) for block in _plan_step_documents(schema)).strip()}

## The comparators

A feedback step without a comparator is a plain read: it answers with whatever the session has, and claims nothing. With one, the step reads until its claim is met or its timeout passes, and a claim that goes unmet fails with what the device did say (the tail of the output, the last frames, or the value that was read), so a wrong claim and a silent board read differently.

{chr(10).join(chr(10).join(("", block)) for block in _plan_comparator_documents(schema)).strip()}

## A plan that runs

Flash, watch the board come up, and stop. `dut` and `dut_uart` are entry names from the authoritative configuration, not fixed words.

```yaml
{PLAN_MINIMAL_EXAMPLE}```

## The same bench, claiming what it read

Stimulus and three claims: a number captured out of a serial line and held to a range, a CAN frame required by identifier and payload, and a symbol in target memory read through the debug session and held to bounds.

```yaml
{PLAN_COMPARATOR_EXAMPLE}```

`uart_write` needs `permissions.allow_write` on that port, `can_send` needs it on the bus and is refused outright on one configured `listen_only: true`, and every symbol a plan reads has to be in `debug.allowed_symbols` unless the configuration sets `debug.allow_all_symbols`. All three are decided at preflight, with nothing opened.
"""


def _resource_descriptor(uri: str, name: str, title: str, description: str, mime_type: str) -> JsonObject:
    return {"uri": uri, "name": name, "title": title, "description": description, "mimeType": mime_type}


MCP_RESOURCES: list[JsonObject] = [
    _resource_descriptor(
        DEBUGGER_BACKENDS_URI,
        "debugger-backends",
        "Required fields per debugger backend",
        "Which of type, executable, probe_id, target_type, interface, interface_cfg, target_cfg, connect_mode and flash_address each of openocd, stlink and pyocd requires, discovers, ignores, or refuses; when probe_id becomes mandatory; when flash_address is needed; which backend can connect under reset; and which of bootstrap discovery's two enumerations answers on a given host, which decides the type and executable a generated entry gets.",
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
        "Which field names the target per backend, known-good values for the Nucleo-F446RE, how pyOCD target types are provided by CMSIS packs, how to find and install the right one and where installed packs live, and what doctor's target_support statuses mean, including why 'undetermined' is not a failure.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        TEST_PLAN_URI,
        "test-plan",
        "How to write a test plan the reactor runs",
        f"Where a plan lives ({DEFAULT_TEST_CONFIG_PATH}), how test_config_path and workspace_root resolve it, and which refusal each mistake earns; which format version admits which step; every step with the entry it routes to and its required and optional keys; the comparator families with their rules; and two plans that run. Generated from the shipped plan schema, not restated.",
        MARKDOWN_MIME,
    ),
    _resource_descriptor(
        TEST_PLAN_SCHEMA_URI,
        "test-plan-schema",
        "Test plan JSON Schema",
        "The bundled JSON Schema every test plan is validated against: every step, key, type, enum, default, and the x-since-version markers the format's version gate reads.",
        JSON_MIME,
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
    if uri == TEST_PLAN_URI:
        return _markdown_content(uri, plan_format_document())
    if uri == TEST_PLAN_SCHEMA_URI:
        return {"uri": uri, "mimeType": JSON_MIME, "text": plan_schema_text()}
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
                "probe_id_at_tool_call_time": UNNAMED_PROBE_RULE,
                "flash_address": FLASH_ADDRESS_RULE,
            },
        )
    return None
