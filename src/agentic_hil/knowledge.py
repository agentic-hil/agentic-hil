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
# The `field` a test-plan refusal names when what is wrong is the document as a
# whole rather than a path into it: the schema validator's own spelling of the
# root, and the scope the loader's three pre-parse refusals (not UTF-8, not
# YAML or JSON, root not a mapping) carry so the catalogue answers them with the
# fault and not with advice about a plan nobody has read.
WHOLE_PLAN_FIELD = "$"
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
# A SocketCAN interface that is on this host and is administratively down. Its
# own error_type rather than a shade of the one above, and the separation is the
# point: an interface that is absent and one that is down are two host states
# with two different fixes, and one answer for both would send an operator
# looking for an adapter that is plugged in and named exactly as configured. The
# kernel lets a CAN_RAW socket bind a down link and only then answers ENETDOWN on
# every receive and every send, so nothing the bind does says what is wrong; the
# state is read before the socket is opened, and the refusal is in the same class
# as a missing interface, with the bench untouched.
CAN_INTERFACE_DOWN_ERROR = "can_interface_down"
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
# The receive queue an opening session drains could not be read out. Named here
# beside the rest of the CAN family because the causes table in `agentic_hil.can`
# is keyed by these names, and a key spelled out as a literal in one place and
# named in another is how the refusal and the classifier come to disagree.
CAN_QUEUE_CLEAR_FAILED_ERROR = "can_queue_clear_failed"
# A frame the adapter would not send. Distinct from `can_read_failed` in what it
# leaves behind: a failed read transmitted nothing, while a failed send may have
# put the frame on the wire before it failed, which is why it carries an unknown
# effect rather than a clean refusal.
CAN_SEND_FAILED_ERROR = "can_send_failed"
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


# What a refusal one permission caused is called, wherever it is raised. Named
# rather than spelled out at each site because the classification is load
# bearing: it is the error_type this project's own agent instructions key on
# (stop, report it, let the operator decide), so a surface answering a denied
# permission with anything else sends the reader down another path. The test
# reactor answered `test_config_invalid`, which says the plan is at fault, for a
# plan that was valid and a configuration that said no (#444).
PERMISSION_DENIED_ERROR = "permission_denied"

# The dotted path one permission is named by, everywhere a refusal is about one.
# `agentic-hil grant` and `agentic-hil revoke` take exactly this spelling, and
# `project_config_describe` reports it, so a refusal that carries it hands the
# operator something to paste rather than a flag name they have to place in a
# file themselves. The short spelling the two commands also accept
# (`debuggers.dut.allow_reset`) is deliberately not what a refusal prints: it is
# a convenience for somebody typing, and a refusal is read by somebody who does
# not yet know which section the entry is in.
#
# `<name>` stands in where the entry has no name to give, which on this surface
# means an unbound debugger. Half a key is still better than none: it says which
# section and which flag, and leaves one blank.
PERMISSION_KEY_PLACEHOLDER = "<section>.<name>.permissions.<key>"
# The two grants that sit directly on a section instead of under a named
# entry's `permissions:` block. They have no `<section>.<name>` half at all,
# so each of these is the whole key, and it is the spelling `agentic-hil grant`
# takes for them.
ALLOW_ALL_SYMBOLS_PERMISSION = "debug.allow_all_symbols"
ARTIFACT_UPLOAD_PERMISSION = "artifacts.allow_upload"


def permission_key(section: str, name: str | None, key: str) -> str:
    """The dotted path `agentic-hil grant` takes for one entry's permission."""
    return f"{section}.{name or '<name>'}.permissions.{key}"


def permission_denied_summary(summary: str, permission: str) -> str:
    """``summary`` with the key that is closed named in it.

    The sentence a person reads first has to carry the key, not only the
    document's fields: an agent told to name the permission it was refused on
    reads the summary out, and a summary that says "disabled by the
    authoritative config" names nothing anybody can act on (#443).
    """
    return f"{summary} The permission is `{permission}` and it is false."


def permission_granted_summary(summary: str, permission: str) -> str:
    """``summary`` with the key that is *open* named in it.

    The exclusivity half. Saying only that a permission is involved would send
    the reader to the grant, which for these is the one move that keeps the
    action refused, so the value is stated with the key.
    """
    return f"{summary} The permission is `{permission}` and it is true."


def permission_denied_next_step(permission: str | None = None) -> str:
    """What to do about a refusal one permission caused, for either reader.

    Deliberately no verb phrase the caller itself can act on. An earlier wording
    said "ask the operator to change the authoritative config" and a small model
    rewrote the config itself to grant `allow_flash`; a caller refused here once
    diagnosed correctly through the other tools and then flashed the board with
    `st-flash`. So the instruction stays "report it and stop", and what changes
    with the key in hand is that the report can name the permission and the
    operator has the line to paste.

    One text for every surface. The command it names is the operator's, at the
    operator's own shell, and saying so is what keeps naming it from reading as
    a route this caller may take: `agentic-hil grant` is reachable from no tool
    on this server.
    """
    named = f", `{permission}`," if permission else ""
    grant = (
        f" The operator opens exactly that key at their own shell with `{CONFIG_GRANT_COMMAND} {permission}`, which "
        "leaves every other key in the file alone; nothing on this surface opens it."
        if permission
        else ""
    )
    return (
        f"This refusal is the answer to the request. Report it and name the permission that is denied{named} then "
        "stop. You must not enable it: the authoritative configuration belongs to the operator and only the operator "
        "may edit it. You must not carry out the action another way either: a debugger, serial device or CAN adapter "
        f"driven outside Agentic HIL defeats the policy this refusal enforces.{grant}"
    )


def permission_denied_fields(permission: str | None) -> JsonObject:
    """The key a permission refusal is about, and the one move it leaves open.

    Merged into a refusal by every surface that raises one, so the field and the
    sentence never come apart. Empty for a refusal that cannot name a single
    key, because a `permission` field naming the wrong one is worse than none.
    """
    if not permission:
        return {}
    return {"permission": permission, "next_step": permission_denied_next_step(permission)}


# The scope for the other kind of permission refusal: one a *granted* key
# causes. `permission_denied` unscoped says a key is false and the operator
# opens it; these say a key is true and the operator closes it, and handing the
# unscoped advice to one of them would send an operator to grant the very flag
# that is blocking them.
EXCLUSIVE_PERMISSION_SCOPE = "exclusive"

# The scope for the test reactor's own reading of an error type that other
# routes answer with too. `cleanup_failed` is also what a debug session says
# when its own teardown fails, and there the move is that session's; a plan
# run's cleanup failure is the run's devices and its recovery, and handing a
# reader the debug session's advice would send them to stop a session that
# belonged to a service that has already closed. Every failed run result names
# this scope, so a type with no scoped entry falls back to its bare one.
TEST_REACTOR_SCOPE = "test_reactor"


def exclusive_permission_fields(blocking: str, debugger_id: str | None) -> JsonObject:
    """The key an exclusivity refusal is about, and the direction it has to move.

    Same `permission` field as every other refusal on this surface, so a caller
    reads one key wherever the answer came from. The advice is the scoped
    entry's, not the unscoped one's, and it is carried on the result rather than
    looked up later: the two cases are one `error_type` and a renderer asking
    the catalogue by error type alone cannot tell them apart.
    """
    key = permission_key("debuggers", debugger_id, blocking)
    return {
        "permission": key,
        "next_step": (
            f"This refusal is the answer to the request. Report it and name `{key}`, which is true and is what blocks "
            f"this, then stop. The operator closes exactly that key from their own shell with `{CONFIG_REVOKE_COMMAND} "
            f"{key}`; no tool on this server is behind that flag, so closing it takes nothing away."
        ),
        **remediation_fields("permission_denied", EXCLUSIVE_PERMISSION_SCOPE, permission=key),
    }


def exclusive_permission_summary(action: str, blocking: str, debugger_id: str | None) -> str:
    """Why an action is refused by a permission that is *granted*, and the fix.

    One text for the four backends that raise it, because a refusal an operator
    meets on their first flash must not read differently depending on which
    programmer their board happens to use.

    It gives the reason rather than restating the rule. "Mutually exclusive
    policies" on its own reads as an arbitrary interlock, and an operator who
    takes it that way reaches for the flag it names, which is the one move that
    keeps flashing refused."""
    entry = permission_key("debuggers", debugger_id, blocking)
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
# The scope every refusal of attached-hardware discovery is looked up under:
# `agentic-hil init`, `agentic-hil adopt-hardware`, `project_config_create` and
# `project_config_adopt_hardware` all read the bench through it, and none of
# them has a configured debugger yet to scope by. The same error_type means a
# different thing there than on a configured bench (no `executable` to correct,
# no probe_id to repoint), so discovery has its own entries where the advice
# differs, and the lookup falls back to the bare entry where it does not.
DISCOVERY_SCOPE = "discovery"
# The two `not_supported` refusals of a configuration that does not settle which
# in-circuit debugger or programmer a tool drives. They share the word with the
# per-backend refusals and nothing else: the way out is a configuration change,
# not another backend, so each has its own scope.
UNBOUND_DEBUGGER_SCOPE = "unbound_debugger"
UNNAMED_PROBE_SCOPE = "unnamed_probe"
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
        "revoke_command": CONFIG_REVOKE_COMMAND,
        "test_plan_reference": TEST_PLAN_URI,
        # The key a `permission_denied` entry is about, which the catalogue
        # cannot know: it is a fact about the refusal in hand, not about this
        # host. The generic shape stands where no refusal is being rendered (the
        # MCP error reference, which serves the entry to a reader who has not
        # met one yet), and `remediation_fields(permission=...)` replaces it with
        # the actual key when a result carries one (#443).
        "permission": PERMISSION_KEY_PLACEHOLDER,
        "reinstall_first_step": reinstall_first_step(),
    }


def _host_locks_running_files() -> bool:
    """Whether this platform refuses to replace the files of a running process.

    The same question `upgrade._host_locks_running_files` asks, spelled again
    here rather than imported: `upgrade` imports this module, so an import the
    other way would be a cycle. A function rather than a constant for the same
    reason it is one there: it is the seam a test replaces to read either side of
    the branch on whichever host the suite happens to run on, and the alternative
    a test reaches for otherwise is writing a platform name into the shared `os`
    module, which this suite has been broken by twice.
    """
    return os.name == "nt"


def reinstall_first_step() -> str:
    """What comes before a reinstall, in the terms of the host it will be run on.

    On a host that refuses to delete a file mapped as a running image, a
    reinstall removes the environment first and that delete fails while the MCP
    server the host started is running out of it: closing the host is not a
    precaution there, it is the difference between a repair and a half-removed
    installation. Everywhere else the old files are unlinked while the processes
    using them keep reading their own copies, so the reinstall goes through with
    a server up and the thing that is true instead is that the server keeps
    answering with the release it imported.

    Both sentences were on one bench at once until now: the Windows one was
    printed as step 1 of the pin refusal on a Linux host, which sent an operator
    to close sessions over a failure their machine does not have. A step whose
    subject is the host has to be chosen by the host, and the catalogue is where
    that choice belongs, because which reader a step is for is part of what the
    step says.
    """
    if _host_locks_running_files():
        return (
            "Close the agent host first. The command below reinstalls the environment, which on this host means "
            "deleting it, and that delete fails while the MCP server the host started is still running out of it. "
            "`agentic-hil upgrade` moves the launcher on PATH out of the manager's way before it runs; a reinstall "
            "typed by hand has nothing of the kind, and the delete it starts with is the step that fails."
        )
    return (
        "The command below can be run with a server up. On this host the old files are unlinked while the process "
        "using them goes on reading its own copy, so nothing fails and nothing is lost. What that process does not "
        "do is pick up the new release: the host that started it still has to be restarted afterwards, and until it "
        "is, that server answers with the release it imported."
    )


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
    "`allow_mass_erase`, which it writes false so that flashing works. That grants flashing, reset, COM and CAN "
    "writes, artifact upload, unrestricted symbol access and all three `permissions.allow_config_*` grants, so the "
    "bench is workable from the file it produces without anyone editing YAML; over that route the two flash "
    "interlocks are false by construction. "
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

# The two steps out of a `config_changed` refusal, named for the same reason: it
# reaches an agent through `hardware_recover` and a person through `agentic-hil
# recover`, and each reader's first move is on their own surface. The override
# is the operator's acceptance of a change they have reviewed, so neither step
# hands it over ready-made: over MCP it is passed once they have confirmed, and
# at a shell they add it to their own line themselves.
_CONFIG_CHANGED_MCP_ROUTE = (
    "Over MCP, show the operator both digests, `recorded_config_sha256` and `current_config_sha256`; once they have "
    "reviewed the change between them and confirm it, call `hardware_recover` again with `accept_config_change: true`."
)
_CONFIG_CHANGED_SHELL_ROUTE = (
    "At a shell, the operator adds `--accept-config-change` to their own `agentic-hil recover` line, and only after "
    "reviewing both digests: the flag is their acceptance of the change, so no command handed to them carries it."
)

# The one remediation step that is a pointer rather than an instruction. It is
# worth its place only where the field it points at exists, and a plan refusal
# that carries no `validation_error.next_step` printed it anyway, telling the
# reader to read a field that was not there (#446). Named here so the rendering
# can recognise the entry's own sentence rather than a copy of it: a renderer
# matching a string it keeps itself would go quiet the day somebody reworded
# this one, which is the failure nobody would notice.
READ_VALIDATION_NEXT_STEP = (
    "Read `next_step` inside `validation_error` first. It names the key that is wrong and the command or the "
    "configuration field that fixes it; the steps below are the general form of the same three answers."
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
            "`description_reload` means the four device sections came from the last explicit "
            "`project_config_reload_description` (`description_reloaded_at`) while values outside those sections and "
            "permissions still came from startup (`loaded_at`). If any such values differ from the file, "
            "`restart_required_for` names them and `loaded_digest` fingerprints the effective description. Nothing "
            "failed; what an answer says and what "
            "the file says have come apart, and the file is the one an operator reads. This says the two digests differ "
            "and nothing more: not what the file now contains, and not what a restart onto it would produce."
        ),
        remediation=(
            "`config_status.state` says which of three this is, and each asks for something different: `changed` means "
            "the file on disk differs from the one this server loaded, and the steps below are for that; `missing` "
            "means the file is gone, so it has to be restored before there is anything to restart onto; `unreadable` "
            "means it is there and will not open, so it has to be made readable first.",
            "If what changed is the device description (`target`, a `debuggers`, `com_ports` or `can_buses` entry, a "
            "probe id, a COM device, a baudrate), call `project_config_reload_description`. It re-reads those four "
            "sections without a restart or touching a permission. Its result names anything outside those sections "
            "that still needs a restart.",
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
            "Do not ask for a restart on `missing` or `unreadable` before the file has been restored or made readable: "
            "a restart reads the file too, and until it can there is nothing to restart onto.",
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
            "Ask the operator to make the file readable again and only then restart the MCP server, so that what is "
            "enforced and what can be read are the same document. A restart before the file is readable again cannot "
            "succeed: startup has to read it too.",
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
            "{reinstall_first_step}",
            "Run the command in `reinstall_command` on this result. It is the one that clears the pin *and* rebuilds "
            "this installation as it stands: `installed_extras` says which extras were found, `with_packages` names "
            "every package `uv`'s own receipt records as installed alongside this one, and `recorded_python` names the "
            "interpreter the install recorded when it recorded one. Reinstalling without any of them removes it: on a "
            "bench with `can` that silently takes CAN support away, and on one created with `--with pytest` it "
            "uninstalls pytest. The hint `uv` prints names the bare distribution and would do exactly that, so use "
            "ours and not that one.",
            "Then run `agentic-hil --version` to confirm the number moved, and start the agent host, which loads the "
            "new server. Nothing was restarted or reloaded by this attempt; there is nothing new to load yet.",
            "Later upgrades work normally: the reinstall records an unpinned requirement, so `agentic-hil upgrade` "
            "moves the installation from then on.",
        ),
        do_not=(
            "Do not report this as an upgrade, and do not ask the operator to restart anything on the strength of it. "
            "The installation still runs the version it ran before, with that version's behaviour and its refusals.",
            "Do not run `uv tool install agentic-hil@latest` from `uv`'s own hint. It names the distribution alone, "
            "and `uv` records the requirement literally, so it uninstalls whatever `[can]` or `[pyocd]` brought in "
            "and every package the receipt records beside it: a bench installed with `--with pytest` loses pytest, "
            "and `Uninstalled 5 packages` is the only notice it gets.",
            "Do not have Agentic HIL rewrite the installation on the operator's behalf, and do not reach for "
            "`--force` to make the pinned upgrade take. Which version a machine runs is the operator's decision; a "
            "pin can be deliberate, and an upgrade that replaces an installation nobody asked it to replace is a "
            "bigger surprise than the one being reported here.",
        ),
    ),
    "upgrade_blocked_by_recorded_option": ErrorRemedy(
        meaning=(
            "The package manager resolved nothing to install and the index publishes a newer release than the one "
            "installed, so the two disagree, and this installation's own receipt says why: `held_back_by` names what "
            "was recorded for it. A `uv tool install --exclude-newer <date>` writes that date into the receipt, and "
            "every later `uv tool upgrade` resolves as of it and prints `Nothing to upgrade` however many releases "
            "have appeared since; `uv` records the option whether it was given as a flag or through the environment, "
            "and offers no command that clears it. A recorded exact `==` requirement does the same thing when the "
            "manager does not say so in words; a `>=` or `~=` floor never does, because a floor resolves to whatever "
            "the index offers above it, and an installation created with one is not reported here at all. Nothing was "
            "changed: no command capable of replacing this installation ran. What was refused is the release itself, "
            "which is why this exits non-zero: `version` is what is installed, `newest_release` is what the index "
            "publishes, and the difference between them is the whole of this result."
        ),
        remediation=(
            "{reinstall_first_step}",
            "Run the command in `reinstall_command` on this result. It records this installation again from what it "
            "is given, without the recorded option, and it carries what the installation already has: the extras in "
            "`installed_extras`, the packages in `with_packages` that the receipt records beside it, and the "
            "interpreter in `recorded_python` where one was recorded.",
            "Then run `agentic-hil --version` to confirm the number moved, and restart the agent hosts, which is what "
            "loads the new server. Nothing was restarted or reloaded by this attempt.",
            "If the option was deliberate, this result is the confirmation that it is still in force and what it now "
            "costs: `newest_release` is the release it is holding out. Leaving it is a decision, not a fault.",
        ),
        do_not=(
            "Do not report this as an installation that is already current. The manager said it had nothing to "
            "install; the index says there is a newer release, and reading the first as the second is exactly what "
            "left a bench a release behind believing it was on the newest one.",
            "Do not run `uv tool upgrade` again, with or without `--reinstall`, to make it take. It resolves through "
            "the recorded option every time, and it is the option rather than the resolution that has to change.",
            "Do not have Agentic HIL rewrite the installation on the operator's behalf. A recorded option can be "
            "deliberate policy about which releases this machine takes, and which release a bench runs is the "
            "operator's decision.",
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
    "upgrade_manager_not_established": ErrorRemedy(
        meaning=(
            "The upgrade could not tell which package manager holds the installation this process runs out of: the "
            "environment directory in `prefix` could not be listed, so whether uv's receipt is in it is unknown. Each "
            "manager's upgrade command replaces the installation, and the one a guess would have run crosses a "
            "recorded exact pin without reading it, so no manager was run. The installation, its version and its "
            "extras are as they were."
        ),
        remediation=(
            "Read `prefix` and `python` on this result: the environment directory that could not be listed, and the "
            "interpreter running out of it.",
            "Have the operator make that directory listable by the account that ran the upgrade; it is usually a "
            "permission or an ownership change on the directory itself.",
            "Run the same upgrade again. With the directory readable, the owning manager is established and it is the "
            "one that runs.",
        ),
        do_not=(
            "Do not upgrade with `pip install --upgrade` or `uv pip install --upgrade` instead. On a uv tool "
            "installation that crosses a recorded exact pin without reading it, reports success, and leaves uv's "
            "receipt naming the old requirement.",
            "Do not reinstall the package or delete the environment to get past this. Nothing was changed, and the "
            "installation works as it did.",
        ),
    ),
    "upgrade_manager_not_found": ErrorRemedy(
        meaning=(
            "The package manager that holds this installation is known, `manager` names it, and it is not on PATH "
            "for the process that ran the upgrade, so it could not be run. Nothing was run and nothing was changed: "
            "the installation, its version and its extras are as they were."
        ),
        remediation=(
            "Put the manager in `manager` on PATH for the process that runs the upgrade, and run the upgrade again. "
            "At a shell that is the shell's own PATH. Over MCP it is the environment the agent host started this "
            "server with, so the host has to be started again from an environment that has it.",
            "If the manager was removed from this machine, install it again first. The installation `python` runs "
            "out of still belongs to it, and only it upgrades that installation with what it recorded.",
        ),
        do_not=(
            "Do not upgrade with another manager, or with pip, in its place. A different manager replaces the "
            "installation without the pin, the extras or the packages the owning one recorded for it.",
            "Do not reinstall the package to get past this. Nothing was changed, and the installation works as it "
            "did.",
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
    # The refusal a configuration that exists but does not load lands on, and
    # the one that went out with nothing attached while every neighbour carried
    # a way forward. It has two causes with opposite fixes and the refusal
    # cannot always tell which it is looking at, so the entry names both and
    # says which field separates them.
    "config_invalid": ErrorRemedy(
        meaning=(
            "The authoritative configuration was found and read and does not match the schema this Agentic HIL "
            "enforces, so nothing was loaded: no bench, no permission, no state directory. `field` is the dotted key "
            "the refusal is about and `path` the file it is in. There are two ways to get here and they have opposite "
            "fixes. One is a mistake in the file, and `rejected_fields` (the keys this schema does not define), "
            "`allowed_fields` (the keys it does), `allowed_values` and `expected_type` are what locate it. The other "
            "is that a newer Agentic HIL wrote this file: `written_by_release` says which release added the keys, "
            "`installed_version` says what is running here, and the summary says so in words. Nothing was changed on "
            "either route."
        ),
        remediation=(
            "Read `written_by_release` first. When the refusal carries it, the file is not wrong and there is nothing "
            "in it to correct: a newer Agentic HIL than the one reading it wrote those keys. Run `agentic-hil "
            "upgrade` on this machine and start Agentic HIL again.",
            "Otherwise fix the file at `field` in `path`. `rejected_fields` names the keys that were thrown out, "
            "`allowed_fields` the keys that section accepts, and a misspelling is usually visible between the two. "
            "`allowed_values` and `expected_type` do the same job where the key is right and what it holds is not.",
            "`agentic-hil schema` prints the whole schema this installation validates against, which is the "
            "authority on what a section may carry when the two field lists are not enough.",
            "Nothing is loaded, so nothing needs recovering and no hardware was touched. Run `agentic-hil doctor` "
            "once the file is corrected or the upgrade is done; it parses the file fresh and reports the next thing "
            "that is wrong with it.",
        ),
        do_not=(
            "Do not delete the offending keys to make a newer release's file load here. They are the policy that "
            "release wrote, and dropping them silently narrows or widens a bench nobody reviewed; upgrade the "
            "installation instead.",
            "Do not write the configuration again from scratch to get past this. `agentic-hil init --force` replaces "
            "the whole file, every narrowed permission included, so it throws away the operator's own decisions in "
            "order to fix one key.",
        ),
    ),
    "config_schema_invalid": ErrorRemedy(
        meaning=(
            "The configuration schema this installation ships is not itself a valid JSON Schema, so no configuration "
            "can be checked against it. The fault is in the installation: the configuration at `path` was never "
            "validated, and nothing in it caused this. `schema_error` is the schema check's own account."
        ),
        remediation=(
            "Reinstall Agentic HIL through the package manager that installed it, with the extras it was installed "
            "with. The one-line installer repairs an existing installation in place: `curl -LsSf "
            "https://agentic-hil.github.io/install.sh | sh`, or in PowerShell `irm "
            "https://agentic-hil.github.io/install.ps1 | iex`.",
            "In a development checkout, restore `src/agentic_hil/schemas/config.schema.json` from version control; "
            "`schema_error` names what in it fails.",
            "Then run `agentic-hil doctor`, which validates the configuration against the repaired schema.",
        ),
        do_not=(
            "Do not edit the workspace configuration to get past this. It was never checked against anything.",
            "Do not copy in a schema from another release. The schema and the code that reads the configuration are "
            "one release's pair.",
        ),
    ),
    "workspace_is_home": ErrorRemedy(
        meaning=(
            "`agentic-hil init` or `agentic-hil setup` ran in the home directory, or in a directory that contains it. "
            "Both bind one authoritative configuration to the directory they run in, and home is not a project: "
            "rooted there, it would govern every project on this machine at once. Nothing was written; `setup` keeps "
            "the user-wide half it had already installed."
        ),
        remediation=(
            "Change into the project this bench belongs to and run the same command again. If the project does not "
            "exist yet, create its directory first (`mkdir my-project`, then `cd my-project`).",
            "The user-wide half needs no project: `agentic-hil agent-install --agent <agent>` installs the skill and "
            "the MCP registration for this user from any directory, home included.",
        ),
        do_not=(
            "Do not point `AGENTIC_HIL_CONFIG` at a configuration whose `workspace_root` is the home directory to get "
            "around this. It binds every project under home to one bench policy, which is what this refusal prevents.",
        ),
    ),
    "config_exists": ErrorRemedy(
        meaning=(
            "Another command wrote this project's authoritative configuration at `path` while `agentic-hil init` or "
            "`agentic-hil setup` was running: the file was not there when the command first looked, and it was there "
            "when the command came to write. Nothing was written over it."
        ),
        remediation=(
            "Run the same command again. It now finds the configuration, keeps it unchanged, and goes on to "
            "`agentic-hil doctor`.",
            "Find out what else was setting this project up at the same moment (a second terminal, a script, an "
            "agent) and let one of them finish.",
        ),
        do_not=(
            "Do not add `--force` to get past this. It regenerates the file from a fresh read and replaces the "
            "configuration the other command just wrote, every narrowed permission included, which is the "
            "operator's decision to take.",
        ),
    ),
    "schema_exists": ErrorRemedy(
        meaning=(
            "`agentic-hil schema --output` or `agentic-hil test-schema --output` named a path where a file already "
            "is, and without `--force` it is not replaced. Nothing was written."
        ),
        remediation=(
            "Read the file at `path`. If it is an earlier copy of the same schema, written by this command for an "
            "editor or a validator, run the command again with `--force` to replace it with this installation's copy.",
            "Otherwise give `--output` a path that is free, or leave `--output` off to print the schema to standard "
            "output.",
        ),
        do_not=(
            "Do not add `--force` over a file you did not write with this command. It replaces the file whole, and "
            "nothing that was in it is kept.",
        ),
    ),
    "mcp_config_exists": ErrorRemedy(
        meaning=(
            "`agentic-hil mcp-config --output` named a path where a file already is, and without `--force` it is not "
            "replaced. Nothing was written. With `--force` the file is written new, holding the agentic-hil server "
            "entry and nothing else."
        ),
        remediation=(
            "Read the file at `path`, usually the project's `.mcp.json`, before anything else: it can hold other "
            "servers the project relies on.",
            "Prefer the user-level registration, which writes no file into the project: `agentic-hil agent-install "
            "--agent <agent>`.",
            "If the file holds nothing but an earlier agentic-hil entry, run the command again with `--force`.",
        ),
        do_not=(
            "Do not add `--force` while the file holds other servers. The file is written new with the agentic-hil "
            "entry alone, and every other server in it is gone.",
        ),
    ),
    "mcp_config_conflict": ErrorRemedy(
        meaning=(
            "The agent's user-level MCP configuration at `path` already has an `agentic-hil` entry that this "
            "installation did not write and cannot attribute to itself; `existing_command`, where present, is what "
            "that entry runs. It was left untouched, no registration was written, and `--force` does not apply to it."
        ),
        remediation=(
            "Report the conflict to the operator, naming the file in `path` and what the entry runs in "
            "`existing_command`, and stop. Whether that entry stays, is replaced or is removed is the operator's "
            "decision.",
            "Once the operator has resolved it in their own file, run the same command again; it then registers the "
            "trusted launcher.",
        ),
        do_not=(
            "Do not edit the file or remove the entry yourself and run the command again. The entry decides which "
            "program an agent hands the hardware gate to, and it belongs to the operator.",
            "Do not add `--force`. It never replaces an entry this installation did not write.",
        ),
    ),
    "skill_conflict": ErrorRemedy(
        meaning=(
            "A skill file already stands at `target_path`, and it is not the Agentic HIL skill this installation "
            "writes. It was left untouched, nothing was installed, and `--force` does not apply to it."
        ),
        remediation=(
            "Report the conflict to the operator, naming the file in `target_path`, and stop. What happens to a skill "
            "this installation did not write is the operator's decision.",
            "Once the operator has moved that file away, run the same command again.",
        ),
        do_not=(
            "Do not overwrite, edit or delete that file yourself and run the command again. It is someone else's "
            "skill, and nobody has decided to replace it.",
            "Do not add `--force`. It never replaces a skill this installation did not write.",
        ),
    ),
    "skill_exists": ErrorRemedy(
        meaning=(
            "The Agentic HIL skill at `target_path` is one this installation wrote, it carries the same version as "
            "the packaged copy at `source_path`, and its text differs: it was edited after it was written, or a "
            "development build changed the packaged text without a new version. Nothing was written."
        ),
        remediation=(
            "Compare the file at `target_path` with the packaged copy at `source_path` to see what differs.",
            "If the difference is not wanted, run the same command again with `--force`, which replaces this managed "
            "file with the packaged copy.",
            "If it is a deliberate local edit, keeping it is the operator's decision, and leaving the file as it is "
            "keeps it.",
        ),
        do_not=(
            "Do not add `--force` over a local edit without the operator's word. It replaces the file whole, and the "
            "edit is gone.",
        ),
    ),
    "unsupported_agent": ErrorRemedy(
        meaning=(
            "The agent named in `agent` or `agents` is not one this installation knows, so it has no skill "
            "directory, MCP configuration format or setup paths for it. Nothing was written. `allowed_agents` lists "
            "the agents it does know, by the name each has here."
        ),
        remediation=(
            "Run the same command again naming one of `allowed_agents`. Each also answers to its common aliases, "
            "such as `claude` for `claude-code`.",
            "For an agent outside that list that reads skills from a directory, `agentic-hil skill-install --agent "
            "<name> --target <path of its skill file>` writes the skill there. Registering the MCP server with that "
            "agent is a step the operator takes in the agent's own configuration; `agentic-hil mcp-config` prints the "
            "command and arguments an entry needs.",
        ),
        do_not=(
            "Do not name a listed agent that the agent in use is not, to get past this. The skill and the "
            "registration would land where that other agent looks, and the agent in use would read neither.",
        ),
    ),
    "agent_permissions_unreadable": ErrorRemedy(
        meaning=(
            "The agent's settings file at `path` cannot be used as it stands: it is not a JSON object, or, for "
            "Claude Code, its `permissions` entry is not an object or its `permissions.deny` entry is not a list. The "
            "file belongs to the agent and the operator, so it was left exactly as it was: no write refusal was added "
            "to it by `init --agent` or `setup`, and none was taken back from it by `uninstall`."
        ),
        remediation=(
            "Open the file at `path` and find what is wrong with it: a syntax error, or one of those two entries "
            "holding another type.",
            "Have the operator repair it in place, keeping the rules and settings it already holds.",
            "Run the same command again.",
        ),
        do_not=(
            "Do not delete or replace the file to get past this. It holds the operator's own settings and rules for "
            "that agent, and a new file throws them away.",
        ),
    ),
    "agent_project_record_unreadable": ErrorRemedy(
        meaning=(
            "A project bound through `AGENTIC_HIL_CONFIG` outside the projects directory has to be named in "
            "`external-projects.json` before a write refusal is written for it, and that record did not read as the "
            "record it has to be: it could not be opened, is not JSON, or is not a JSON object whose `configurations` "
            "is a list of absolute paths. A record that may name projects and cannot be read is no ground to write "
            "rules from, so this project was not recorded, no deny rule was written, and the file was left untouched. "
            "`path` is the record this user's commands write to; a second copy can stand beside the other "
            "configuration root, and an unreadable copy there refuses the same way."
        ),
        remediation=(
            "Open `external-projects.json` at `path`, and the copy beside the other configuration root if there is "
            "one, and find the one that does not read: a file this account cannot open, a syntax error, or an entry "
            "that is not an absolute path.",
            "Have the operator repair that file in place, keeping every path it names: a JSON object whose "
            "`configurations` key holds a list of absolute configuration paths.",
            "Run the same command again.",
        ),
        do_not=(
            "Do not delete the record to get past this. The projects it names would read as gone, and a later setup "
            "would take back the write refusals that protect them.",
        ),
    ),
    "agent_project_record_unwritable": ErrorRemedy(
        meaning=(
            "A project bound through `AGENTIC_HIL_CONFIG` outside the projects directory has to be named in "
            "`external-projects.json` before a write refusal is written for it, and the record could not be written "
            "at `path`; the summary carries the error. No deny rule was written, because a rule for a project the "
            "record does not name is one a later run reads as nobody's and takes back."
        ),
        remediation=(
            "Read the error in the summary: it says why the write was refused, a permission, a read-only location, "
            "or no usable configuration root at all.",
            "Have the operator make the directory that holds `path` writable for this account, or clear the cause the "
            "error names.",
            "Run the same command again.",
        ),
        do_not=(
            "Do not write the deny rule into the agent's settings by hand. Without the record no run can tell whose "
            "it is, and a later setup takes it back.",
        ),
    ),
    "agent_project_record_unremovable": ErrorRemedy(
        meaning=(
            "`agentic-hil uninstall` took back the write refusals this installation wrote and then could not remove "
            "a record of projects it wrote, `external-projects.json`. `failed` names each file still standing, with "
            "the error that refused its removal. The record names projects and grants nothing, so what is left is a "
            "file, not a refusal in force."
        ),
        remediation=(
            "Read `failed`: each entry is a record that is still there and the error that kept it there, a "
            "permission, a read-only mount or an I/O error.",
            "Have the operator clear that cause and delete the file, or run `agentic-hil uninstall` again once it is "
            "cleared.",
        ),
        do_not=(
            "Do not read this as write refusals still in force. They were taken back before the record was reached.",
        ),
    ),
    "mcp_command_untrusted": ErrorRemedy(
        meaning=(
            "No Agentic HIL executable passed the check every MCP registration is written from, so nothing was "
            "registered. The launcher has to be an absolute path outside this project and outside temporary and "
            "cache directories, and stay the same file while it is checked. On Linux and macOS it also has to be a "
            "regular file or one launcher symlink to one, owned by this account or root, executable, writable by no "
            "other account, in a directory owned by this account or root. `rejected_candidates` names every launcher "
            "that was tried and why each failed. A refusal of one path carries `path`; an executable refused for its "
            "owner, its write access or a missing execute bit adds `untrusted_because`, `mode`, `uid` and `gid`; a "
            "launcher whose parent directory belongs to an account other than this one or root adds `directory`, "
            "naming that parent directory, with its `mode` and `uid`; a launcher symlink whose target resolves "
            "through another symlink adds `target`."
        ),
        remediation=(
            "Read `rejected_candidates`, or `path` and the fields beside it, for the reason each launcher failed.",
            "A launcher in the project, a temporary directory or a cache (a `uvx` or one-off run) cannot be "
            "registered: install Agentic HIL persistently with `uv tool install agentic-hil` or `pipx install "
            "agentic-hil`, and run the command again from that installation.",
            "For an owner or a mode, have the operator fix what the refusal names: give the file at `path`, or the "
            "directory at `directory`, to this account, `chmod go-w` the file where other accounts can write it, "
            "and `chmod +x` it where it is not executable. Then run the command again.",
            "A launcher that changed while it was checked was being replaced at that moment; run the command again "
            "once the installation has finished.",
        ),
        do_not=(
            "Do not register the bare command name or a relative path in the agent's configuration by hand. Which "
            "program it starts would then depend on the directory and PATH the agent happens to have.",
            "Do not point the registration at a copy inside the project. The project is the one tree the hardware "
            "gate cannot trust.",
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
    # The unscoped entry the four above fall back to, and the one every device
    # permission lands on: a probe's `allow_reset`, a port's `allow_write`, a
    # bus's `allow_read`. It carries `{permission}`, which is not a fact about
    # this host and so not one `_substitutions()` can supply; the refusal in hand
    # supplies it through `remediation_fields(permission=...)`, and the generic
    # shape stands where the entry is read on its own. Until this existed, the
    # most common refusal on the whole surface answered with no advice at all
    # and named no key, so an agent told to report the permission it was denied
    # had nothing to report and an operator had nothing to paste (#443).
    "permission_denied": ErrorRemedy(
        meaning=(
            "The authoritative configuration does not grant this action on this entry, so nothing was locked, opened "
            "or driven and there is nothing to clean up. `permission` names the key, in the spelling the file uses "
            "and the operator's commands take: `{permission}`. This is a decision somebody made about this bench, not "
            "a fault in it and not a state that clears itself, so the same call refused now is refused on every "
            "retry until an operator moves that key."
        ),
        remediation=(
            "Report the refusal and name `{permission}`, the key it is about. That is the whole of what this surface "
            "can do about it, and it is what makes the refusal actionable for whoever owns the bench.",
            "The operator opens exactly that key from their own shell with `{grant_command} {permission}`. It leaves "
            "every other key in the file alone, and it is reachable from no tool on this server.",
            "`project_config_describe` says which permissions this entry does grant right now, so the part of the "
            "task that is possible is not abandoned along with the part that is not.",
        ),
        do_not=(
            "Do not edit the authoritative configuration to grant it. `project_config_set` writes only `false` into a "
            "permission, whatever else it may write, and a file edited with your own tools is the exact move the "
            "host deny rules `agentic-hil setup` installs exist to stop.",
            "Do not carry the action out another way. A debugger, serial device or CAN adapter driven outside "
            "Agentic HIL reaches the same hardware with the policy and the audit trail both stepped around, and that "
            "is what this refusal is for.",
            "Do not run `{reopen_command}` to get past it. That rewrites the whole file from hardware discovery, so "
            "it takes every other narrowing, the baudrate, the `resource_id`, the `state_root` and the artifact roots "
            "with it: a reset, not a repair.",
        ),
    ),
    f"permission_denied:{EXCLUSIVE_PERMISSION_SCOPE}": ErrorRemedy(
        meaning=(
            "The same `error_type` for the opposite state: `{permission}` is **true**, and this action is refused "
            "because it is. `allow_raw_debugger_commands` and `allow_mass_erase` act on flash outside the path this "
            "server validates, so while either is open a flash report's claim about what is on the device is not one "
            "this server can stand behind, and validated flashing and unrestricted debugger access are mutually "
            "exclusive policies rather than an arbitrary interlock. Nothing was sent to the target."
        ),
        remediation=(
            "Report the refusal and name `{permission}`, the key that is open and is what blocks this. Naming the "
            "wrong direction is the whole risk here: an operator told only that a permission refused this reaches "
            "for the grant, which is the one move that keeps it refused.",
            "The operator closes exactly that key from their own shell with `{revoke_command} {permission}`. No tool "
            "on this server is behind that flag, so nothing becomes unavailable by closing it, and a generated "
            "configuration leaves both of these false for this reason.",
            "`project_config_describe` reports the key's current value, so whether this bench is in that state is "
            "read rather than guessed at by trying the action again.",
        ),
        do_not=(
            "Do not ask for `{permission}` to be granted, or treat it as the missing grant. It is already granted; "
            "that is the refusal.",
            "Do not flash or erase through the raw debugger the open flag allows. That is precisely the unvalidated "
            "path whose existence made this refusal necessary.",
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
    "config_changed": ErrorRemedy(
        meaning=(
            "The authoritative configuration changed after this incident was recorded: `recorded_config_sha256` is the "
            "digest it was recorded under and `current_config_sha256` the one this server holds now. The recorded "
            "configuration defined the resources, permissions and limits the incident happened under; clearing it "
            "under another one would clear an incident nobody has assessed against the change, so nothing was cleared."
        ),
        remediation=(_CONFIG_CHANGED_MCP_ROUTE, _CONFIG_CHANGED_SHELL_ROUTE),
        cli_remediation=(_CONFIG_CHANGED_SHELL_ROUTE, _CONFIG_CHANGED_MCP_ROUTE),
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
            "two is refused for the widening and loses the narrowing with it. Narrowing is the one direction open "
            "here, and it is made on the operator's word.",
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
            "Do not delete or move the configuration to get a different one: a regeneration over the gap undoes every "
            "narrowing in it, which is this same widening by another route.",
        ),
    ),
    "debugger_not_executable": ErrorRemedy(
        meaning=(
            "The toolchain executable this entry names is present on this host and will not run. The path resolves, it "
            "is a regular file, and the operating system refused to execute it: either its mode withholds the execute "
            "bit, or its contents are not something this machine can run as a program. Nothing was spawned, so the "
            "bench was not touched and the board is where the last call that did reach it left it."
        ),
        remediation=(
            "Read `not_executable_reason` in the refusal, because the two cases have different repairs. "
            "`permission_denied` means the file is there and may not be executed, which is what a toolchain unpacked or "
            "copied out of an archive that did not carry its execute bit looks like: restore it with `chmod +x` on the "
            "path the refusal names, and check that the filesystem it lives on is not mounted `noexec`.",
            "`not_an_executable_image` means the file was reached and is not a program this machine can run: a script "
            "whose first line is not a shebang, an archive or installer configured instead of the binary it unpacks, or "
            "a binary built for another architecture. `file <path>` on the path the refusal names says which, and the "
            "fix is to point the configuration at the real binary for this machine.",
            "Correct `debuggers.<name>.executable` (or `debug.gdb_executable` where the refusal names GDB) with "
            "`project_config_set`, then run `agentic-hil doctor`, which repeats this check and reports the toolchain's "
            "version once the file runs.",
        ),
        do_not=(
            "Do not copy the toolchain binary into the workspace to get a copy you can change. A configured executable "
            "inside the workspace is repository-controlled code running as the debugger, and the configuration refuses "
            "such a path at load.",
            "Do not run the debugger by hand to get past it. The refusal is about the file this configuration names, "
            "and a different binary that happens to be on PATH would leave the bench describing a toolchain it does not "
            "drive.",
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
            "untouched. The hold can also be a lease a call could not give back, which `open_holds.leases_under_incident` "
            "names: it stays registered under the incident holding it until that incident ends."
        ),
        remediation=(
            "Finish the run and close it with `bench_run_stop`, stop any COM or CAN session with "
            "`com_session_stop` / `can_session_stop` and any debug session with `debug_stop_session`, then repeat the "
            "configuration change.",
            "`bench_run_status` says whether a run is open and which devices it declared; the refusal carries the "
            "same in `open_holds`.",
            "A lease `open_holds.leases_under_incident` names belongs to no run and no session, and no stop call frees "
            "it: it goes back when the incident it is registered under ends. `agentic-hil lease-status` names that "
            "incident and whether it stands; repeat the configuration change once it shows no open lease.",
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
            "this call writes nothing, and a caller should not be told it did. The hold can also be a lease a call could "
            "not give back, which `open_holds.leases_under_incident` names: it stays registered under the incident "
            "holding it until that incident ends."
        ),
        remediation=(
            "Finish the run and close it with `bench_run_stop`, stop any COM or CAN session with "
            "`com_session_stop` / `can_session_stop` and any debug session with `debug_stop_session`, then repeat the "
            "reload.",
            "`bench_run_status` says whether a run is open and which devices it declared; the refusal carries the "
            "same in `open_holds`.",
            "A lease `open_holds.leases_under_incident` names belongs to no run and no session, and no stop call frees "
            "it: it goes back when the incident it is registered under ends. `agentic-hil lease-status` names that "
            "incident and whether it stands; repeat the reload once it shows no open lease.",
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
    # The refusal that stops a bench outright had no entry at all, while the
    # retryable neighbour below has had one for as long as it has existed. Two
    # entries and not one, in the scoped form the lookup already supports: an
    # incident this project can clear and one it cannot need opposite advice,
    # and a single entry would send half its readers to a command that answers
    # `nothing_to_recover` by design (#531).
    "resource_quarantined": ErrorRemedy(
        meaning=(
            "A hardware resource is held by an unresolved incident, so nothing was touched. An incident is what a "
            "call leaves behind when it could not confirm what it did to the board, and it outlives the process that "
            "raised it on purpose: the record is what keeps the next caller off a bench nobody can vouch for. "
            "`cleanup_reasons` names what is unconfirmed and `quarantine_guidance` says, per reason, what was "
            "attempted, what still holds, what nobody on this host can know, and what to check on the board. "
            "`quarantine_id` identifies this incident and changes when a new one is raised."
        ),
        remediation=(
            "Read `incident_stands` first: only an incident that stands owes a signature. For one that does not, "
            "`auto_recoverable` says how the next hardware call ends it: true, that call settles it on the evidence it "
            "reads back; false, no recovery action this bench allows can, so that call stands it down unconfirmed. "
            "Either way nothing is signed, so make the call again.",
            "Where a signature is owed, read `quarantine_guidance` and check the board against it, then run "
            "`agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>` with the id "
            "`agentic-hil lease-status` reports right now.",
            "If that id has changed since the refusal, a newer incident has replaced this one. Read `lease-status` "
            "again and sign for the incident it names, never for the one an older result carried.",
        ),
        do_not=(
            "Do not delete the coordination record to get past this. It is the only thing keeping a second caller "
            "off a board whose state nobody has confirmed, and removing it turns an incident into a silent one.",
            "Do not sign `--confirm-safe-state` without looking at the bench. The flag attests a physical state, and "
            "that is the one claim no process on this host can make on the operator's behalf.",
        ),
    ),
    "resource_quarantined:foreign_project": ErrorRemedy(
        meaning=(
            "The resource is held by an incident belonging to a different project on this machine, so this workspace "
            "cannot clear it and the advice is the opposite of the local case. `project_resource` names the owning "
            "workspace as the digest `agentic-hil lease-status` prints for it, which is how the two are matched "
            "without a path leaving either one. `auto_recoverable`, where it is present, is derived under that "
            "project's own recovery policy and probe grants rather than this one's; absent, the record was written "
            "by a version that did not record what its policy permitted, and no claim is made rather than a stale "
            "one."
        ),
        remediation=(
            "Find the workspace whose `agentic-hil lease-status` reports the same `project_resource` digest. That is "
            "the only place this incident resolves.",
            "With `auto_recoverable: true`, nothing has to be signed at all: any hardware call made in that "
            "workspace stands the incident down on its own evidence, and this bench is free straight after it.",
            "With `auto_recoverable: false`, an operator has to check the board as `quarantine_guidance` describes "
            "and run `agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>` in that workspace.",
            "`agentic-hil lease-status` and `agentic-hil doctor` here both report it under `standing_incidents`, so "
            "the bench can be diagnosed from either side.",
        ),
        do_not=(
            "Do not run `agentic-hil recover` in this workspace expecting it to help. Recovery requires a matching "
            "project on the record and on every marker, so it answers `nothing_to_recover` here by design.",
            "Do not delete the other workspace's coordination record, and do not point this configuration at "
            "different devices to get around it. The board is shared, and the incident is about the board.",
        ),
    ),
    # The coordinator's own refusals. They reach a caller through
    # `hardware_recover`, `agentic-hil recover`, `agentic-hil lease-status` and
    # every hardware tool whose lock the coordinator takes, so each entry has to
    # be true at all of those doors and names the payload field that tells the
    # cases apart where one type covers several.
    "resource_busy": ErrorRemedy(
        meaning=(
            "A lock this call needs is held, so nothing was driven and nothing changed. The payload says whose hold "
            "it is. A refusal that carries `resources` met the machine-wide lock of another Agentic HIL process or "
            "command (or a lock file that could not be opened at all, which `backend_error` then says), and "
            "`resources` lists what this call asked for, not who holds it. A refusal with no `resources` was refused "
            "by this server or command itself: a debugger tool while this server's own debug session holds the "
            "debugger, or a recovery while this server still holds a lease of its own."
        ),
        remediation=(
            "When the refusal carries `resources`, call `hardware_lease_status`: `owner_active` says whether a live "
            "owner holds the project, and `device_holds` names the runs holding devices. Wait for that owner to end, "
            "or stop it (`test_reactor_stop` with its handle for a plan run), then call again; the retry is safe.",
            "When a debugger tool is refused with no `resources`, this server's own debug session holds the "
            "debugger. End it with `debug_stop_session`, then call the tool again.",
            "When a recovery is refused with no `resources`, this server still holds a session or a run of its own. "
            "End it (`debug_stop_session`, `com_session_stop`, `can_session_stop` or `bench_run_stop`), then "
            "recover again.",
        ),
        do_not=(
            "Do not delete lock files to get past this. The hold belongs to a live owner, and removing it lets two "
            "owners drive one board.",
            "Do not retry in a tight loop. The hold ends when its owner ends it, and polling does not shorten it.",
        ),
    ),
    "coordination_closed": ErrorRemedy(
        meaning=(
            "The hardware coordinator this call went to has been closed, which happens only while the server or "
            "command that owns it shuts down. Nothing was locked or driven, and nothing changed."
        ),
        remediation=(
            "Start the server or the command again and make the call against the new one; the retry is safe.",
        ),
        do_not=(
            "Do not keep calling the server that is shutting down. Its coordinator does not reopen, so every call "
            "to it answers the same way.",
        ),
    ),
    "operator_confirmation_required": ErrorRemedy(
        meaning=(
            "A recovery was asked for without the operator's confirmation that the board is in a safe state, so "
            "nothing was cleared and the quarantine stands. Clearing a quarantine attests a physical state, and only "
            "a person at the bench can make that claim."
        ),
        remediation=(
            "The operator checks the board as `quarantine_guidance` describes, then runs "
            "`agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>` with the id "
            "`agentic-hil lease-status` reports.",
            "Over MCP, `hardware_recover` carries the confirmation as `operator_statement`: ask the operator what "
            "state the bench is in and pass their answer in their words.",
        ),
        do_not=(
            "Do not confirm a safe state nobody has looked at. The confirmation is written to the recovery ledger as "
            "the operator's, and it is the one claim no process can make for them.",
        ),
    ),
    "coordination_state_invalid": ErrorRemedy(
        meaning=(
            "A coordination record this call depends on is not one it can trust, so the call stopped at that "
            "record. The fields say which record and why. `resource` names a lease or "
            "project record that could not be read (`error_class` and `errno` say what the operating system "
            "answered), that is not JSON (`backend_error`), that was written by another version, that has fields "
            "of the wrong type, or that belongs to a different unresolved project incident. A recovery says its "
            "incident markers are inconsistent. `unlockable_lock_keys` is a declared device whose lock key the "
            "machine-wide mutex does not lock, refused before the run took anything.\n\n"
            "The same type inside an `audit_error` after a hardware action is the canonical audit ledger under "
            "`state_root`: its digest sidecar is corrupted or has an invalid format, or the ledger's size disagrees "
            "with the sidecar. There the action itself already ran; what failed is its evidence, and `audit_ok` is "
            "false on the result."
        ),
        remediation=(
            "Read which record the refusal names and why: `resource` with `error_class` and `errno`, "
            "`backend_error`, or the summary.",
            "A permission, a full disk or a file another program holds open is fixed where it is, and the same call "
            "then reads the record again; nothing else has to change.",
            "A record that is corrupted, from another version or inconsistent is the operator's to judge: no command "
            "rewrites a coordination record or the canonical ledger, and `agentic-hil lease-status` and "
            "`agentic-hil recover` stop on the same record. Hand the operator the refusal as it is, with the record "
            "it names.",
            "`unlockable_lock_keys` is a defect in a device kind, not a bench fault: report it with the plan that "
            "declared the device.",
        ),
        do_not=(
            "Do not delete or hand-edit the coordination records, the canonical ledger or its digest sidecar to get "
            "past this. They are what keeps a second owner off a board nobody has confirmed, and an edited ledger is "
            "evidence that can no longer be checked.",
            "Do not move `state_root` to start from empty records. Every incident and hold under the old root would "
            "become invisible while the hardware it describes stays where it is.",
        ),
    ),
    "quarantine_id_required": ErrorRemedy(
        meaning=(
            "The recovery named no quarantine id, so nothing was cleared. A recovery signs for one incident by its "
            "id, so that a signature never clears an incident nobody looked at."
        ),
        remediation=(
            "Read `quarantine_id` from `agentic-hil lease-status` and pass it: "
            "`agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>`.",
        ),
        do_not=(
            "Do not guess an id or reuse one from an older result. An id names one incident, and a newer incident "
            "gets a new one.",
        ),
    ),
    "resource_not_quarantined": ErrorRemedy(
        meaning=(
            "The recovery found no quarantined incident on this project, so there was nothing to clear and nothing "
            "changed. Usually another recovery or the next hardware call already cleared it between the status read "
            "and this recovery."
        ),
        remediation=(
            "Call `hardware_lease_status`. With nothing standing, carry on with the work the incident held up.",
            "An incident listed under `standing_incidents` belongs to another project and resolves only in that "
            "project's workspace.",
        ),
        do_not=(
            "Do not sign again for an incident that is no longer there, and do not delete coordination records to "
            "make the answer change.",
        ),
    ),
    "quarantine_changed": ErrorRemedy(
        meaning=(
            "The incident this recovery signed for is not the one on record, so nothing was cleared and the "
            "quarantine stands. With no `resource`, the project's incident has another id now (a newer incident "
            "replaced it) or belongs to another project. With `resource`, the incident id matched but that "
            "resource's marker is missing or disagrees with it: another state, another id, another project, another "
            "configuration, or a resource list that does not match the incident's."
        ),
        remediation=(
            "Read `agentic-hil lease-status` again, check the board against the incident it names now, and sign for "
            "that `quarantine_id`.",
            "When the refusal names a `resource`, that marker is the disagreement. If it is another project's "
            "incident on the same device, `standing_incidents` names it and it resolves in that project's workspace "
            "first.",
            "When the same id is refused again on the same `resource`, nothing on this side reconciles that marker: "
            "hand the operator the refusal with the `agentic-hil lease-status` output.",
        ),
        do_not=(
            "Do not edit or delete the marker to make it match, and do not sign again with the old id.",
            "Do not sign for the new id without looking at the board. A newer incident is a new state to check.",
        ),
    ),
    "recovery_audit_failed": ErrorRemedy(
        meaning=(
            "The recovery's line could not be written to the recovery ledger under `state_root`. The ledger line is "
            "written before any marker is released, so nothing was cleared: the quarantine stands under the same "
            "`quarantine_id`. `backend_error` says what the write answered."
        ),
        remediation=(
            "Fix the cause `backend_error` names (a permission, a full disk, a file held open elsewhere), then run "
            "the same recovery again with the same `quarantine_id`.",
        ),
        do_not=(
            "Do not clear the quarantine some other way. A recovery that is not in the ledger is one nobody can "
            "account for later.",
            "Do not move `state_root` to get a writable ledger. The incident lives under the old root and would only "
            "become invisible.",
        ),
    ),
    "recovery_persist_failed": ErrorRemedy(
        meaning=(
            "The recovery is in the ledger, but not every marker it releases could be written, so the quarantine "
            "stands. `backend_error` says what the write answered. A failure after the project was marked "
            "`recovery_pending` leaves it there; a failure on that mark itself leaves the project in the state it "
            "was in."
        ),
        remediation=(
            "Fix the cause `backend_error` names, then run the recovery again with the same `quarantine_id`; the "
            "retry is safe. A project left `recovery_pending` resumes from there, and the rerun's ledger line says "
            "`resumed`.",
        ),
        do_not=(
            "Do not treat the bench as recovered. Until the rerun completes, the quarantine stands.",
            "Do not delete markers to finish the recovery by hand.",
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
            "A refusal that carries no `holder` is the same hold by an owner whose record does not name it yet: wait for "
            "it the same way, and do not take the missing heartbeat for a hang.",
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
            "and the entry's `device` is simply out of date. Set `com_ports.<name>.device` to `expected_device` with "
            "`project_config_set`, or have the operator edit the configuration, then call "
            "`project_config_reload_description` and `com_session_start` again. `next_step` names the key and the value.",
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
            "Plug the board in and run `project_config_adopt_hardware`. It fills `com_ports.<name>.device` in "
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
    "com_port_not_configured": ErrorRemedy(
        meaning=(
            "The `port_id` names no entry under `com_ports` in the authoritative configuration, so there was nothing "
            "to open. Nothing was opened, contacted or written. `configured_ports` lists the names this configuration "
            "does declare. A name that is declared but has no `device` yet is answered with `com_port_not_bound` "
            "instead, so this refusal means the name in the call is not one the project declares."
        ),
        remediation=(
            "Call the tool again with one of the names in `configured_ports`. A name copied from a test plan or an "
            "older configuration that is not in that list is the mistake to fix.",
            "If the port really belongs to this project, the authoritative configuration has to declare it under "
            "`com_ports`, which is the operator's file to change; `agentic-hil com-ports` lists the devices this host "
            "has to bind it to.",
        ),
        do_not=(
            "Do not pass a device name (`COM7`, `/dev/ttyACM0`) as `port_id`. The tools reach a port only by its "
            "configured name, which is what ties it to its permissions and its identity check.",
        ),
    ),
    "com_port_open_failed": ErrorRemedy(
        meaning=(
            "Opening the configured device failed, so no session was started. `backend_error` is the line the open "
            "failed with, and `likely_causes` reads it: on a POSIX host a permission refusal (`EACCES`) means this "
            "user may not open the device node. On Windows a port another program holds is refused here too, because "
            "that refusal carries no number to tell it apart; on POSIX the same case is `com_port_busy`. No session "
            "was registered and the port is not held for it. With `retry_safe` true the failed open left no handle "
            "behind. With `cleanup_error` present, a handle the failed open left standing would not close, and that "
            "is recorded under `cleanup_reasons`."
        ),
        remediation=(
            "Read `backend_error` and `likely_causes` first: they say whether the device is missing, held by another "
            "program, or closed to this user.",
            "On Linux, a permission refusal is fixed by adding the user to the group that owns the device (`dialout` "
            "on Debian and Ubuntu, `uucp` on Arch and Fedora) and logging in again; `ls -l` on the device shows its "
            "group.",
            "A missing device means the adapter is unplugged or the host lists it under another name: plug it in, and "
            "`agentic-hil com-ports` shows what this host lists right now. On Windows, close the terminal, IDE serial "
            "monitor or flashing tool that holds the port.",
            "With `cleanup_error` present, call `com_session_start` again once the cause is fixed: that open is what "
            "settles the recorded handle, since the operating system refuses it if the handle is really still held.",
        ),
        do_not=(
            "Do not switch the entry to a different device just because that one opens. A device name is an "
            "enumeration order, so another name that opens is usually another board.",
        ),
    ),
    "serial_backend_not_available": ErrorRemedy(
        meaning=(
            "pyserial, the backend Agentic HIL reaches serial ports through, could not be imported in the process that "
            "answered. `backend_error` is the import's own line with its type: `ModuleNotFoundError` means the package "
            "is not installed in that environment, any other type means it is installed and failed inside its own "
            "imports. Nothing was opened or contacted. Under `available_com_ports` or `com_ports` this is only the "
            "host listing missing; a `com_ports` entry that names hardware (`serial_number`, `vid`, `pid` or "
            "`resource_id`) is then refused by `com_session_start` as `com_port_identity_unverified` with the identity "
            "status `backend_unavailable`, not with this type."
        ),
        remediation=(
            "Install Agentic HIL with its runtime dependencies into the environment that answered, the one the MCP "
            "server or the `agentic-hil` command runs from. pyserial is one of those dependencies, so a complete "
            "install brings it.",
            "Restart the MCP server afterwards, so that it runs on the installed package, then call the tool again. "
            "The `agentic-hil` command needs no restart: run it again.",
            "If `backend_error` is not `ModuleNotFoundError`, pyserial is present and broken: reinstall it in that same "
            "environment.",
        ),
        do_not=(
            "Do not install pyserial into a different Python environment than the one that runs the server. The "
            "server imports only from its own interpreter, so the refusal stays exactly as it is.",
        ),
    ),
    "com_port_discovery_failed": ErrorRemedy(
        meaning=(
            "Enumerating the host's serial ports raised an operating system error, so this is a listing that could "
            "not be taken, not a finding that no port is attached. `backend_error` is that error. Nothing was opened. "
            "While enumeration fails, a `com_ports` entry that names hardware cannot be checked against what is "
            "behind its device name, and `com_session_start` refuses it as `com_port_identity_unverified`."
        ),
        remediation=(
            "List again with `com_ports_list` or `agentic-hil com-ports`. `likely_causes` names a USB serial driver "
            "whose state changed during discovery, and a second listing a moment later shows whether that has passed.",
            "If every listing fails, look at the host's USB serial driver rather than at the configuration: reconnect "
            "the adapter, or reinstall its driver.",
        ),
        do_not=(
            "Do not remove `serial_number`, `vid` or `pid` from a `com_ports` entry so that it opens without the "
            "check. The listing is what proves the device name still leads to the right board, and a failed listing "
            "proves nothing either way.",
        ),
    ),
    "com_port_identity_unverified": ErrorRemedy(
        meaning=(
            "This `com_ports` entry names its hardware, so it is opened only after the host confirms that its device "
            "name still leads to that hardware, and that check could not run. It is not a mismatch: nothing was found "
            "to be the wrong board, there was no way to tell. The port was not opened and nothing was written. "
            "`identity.status` says which way the check had no answer: `backend_unavailable` (the host's serial ports "
            "could not be listed), `port_not_enumerated` (the listing does not name this device exactly once), "
            "`serial_unknown` (the port reports no serial number to compare) or `usb_ids_unknown` (the port reports "
            "no USB vendor and product id while the entry names them). `retry_safe` is true."
        ),
        remediation=(
            "Read `identity.status` and `identity.summary`, and restore the check that status names: install the "
            "serial backend for `backend_unavailable`; plug the board in, or check that `device` is the name this host "
            "lists for it, for `port_not_enumerated`; use an adapter and driver that report the missing serial or USB "
            "ids for `serial_unknown` and `usb_ids_unknown`.",
            "Then call `com_session_start` again. Nothing was touched, so there is nothing to recover.",
        ),
        do_not=(
            "Do not delete `serial_number`, `vid` or `pid` from the entry to get it opened. Drop them only if the "
            "entry genuinely names no fixed board; removing them to silence this refusal opens a name that nothing "
            "checks.",
        ),
    ),
    "com_reader_start_failed": ErrorRemedy(
        meaning=(
            "The port was opened, but the background reader that buffers its input could not be started, so the "
            "session was not kept. The port was closed again and `cleanup_confirmed` is true. Nothing was written to "
            "the line by this call, although the open applied the entry's `assert_dtr` and `assert_rts` as every "
            "open does. `backend_error` says why the reader would not start."
        ),
        remediation=(
            "Call `com_session_start` again: the port was closed cleanly, so a new start begins from nothing.",
            "If it fails the same way again, `backend_error` names what the server process could not do, and "
            "restarting the MCP server is the repair.",
        ),
        do_not=(
            "Do not read the port with another serial program in the meantime. It would hold the device, and the next "
            "`com_session_start` could not open it.",
        ),
    ),
    "com_port_close_failed": ErrorRemedy(
        meaning=(
            "Closing the port, or stopping its reader, did not confirm, and `backend_error` says which part failed and "
            "how. The session remains registered so that the close can be retried: until it is, `com_read` and "
            "`com_write` on this port answer `session_not_active`, and `com_ports_list` shows it with "
            "`session_active` false. The failure is recorded under `cleanup_reasons`. `quarantined` is true only when "
            "the audit log broke as well; then the incident stands until an operator recovers it, and the handle, "
            "once closed, is no longer retried."
        ),
        remediation=(
            "Call `com_session_stop` again with the same `port_id`. The close is retried from the registered session, "
            "and a stop that succeeds releases the port. If an incident this call may not end holds the lease, the "
            "stop answers `session_lease_held` instead and names the call that ends it.",
            "`com_session_start` on the same port retries the close as well, and opens a fresh session once it "
            "succeeds.",
            "Read `quarantine_guidance` for what the failed close leaves unconfirmed. If `quarantined` is true, fix "
            "the audit destination first, then follow that guidance to the operator's signature.",
        ),
        do_not=(
            "Do not sign `agentic-hil recover --confirm-safe-state` for this while `quarantined` is false. Nothing is "
            "held for a signature: the next stop or start settles the handle, and the operating system refuses that "
            "open by itself if the handle is really stuck.",
        ),
    ),
    "session_lease_held": ErrorRemedy(
        meaning=(
            "The session's handle is closed, but its lease could not be given back: an open incident holds it, and "
            "this call may not end that incident. Inside a bench run, the run's teardown ends it; outside a run, a "
            "debug session that is still open keeps it. The session stays registered for nothing but its lease, "
            "nothing was sent to the device, and `cleanup_confirmed` is never true here because the lease is still "
            "held. `next_step` names the call that ends the incident; a CAN refusal carries the `participant`."
        ),
        remediation=(
            "Follow `next_step`: inside a run, call `bench_run_stop`; otherwise end the session that holds the "
            "incident open, a debug session with `debug_stop_session`.",
            "Then call the stop again with the same arguments. It gives the lease back without touching the device "
            "again, and a start after it opens a fresh session.",
        ),
        do_not=(
            "Do not open the device from another program in the meantime. The resource stays reserved for this "
            "session until the stop that follows the incident's end.",
            "Do not call the stop in a loop. The answer stays the same until the call `next_step` names has run.",
        ),
    ),
    "serial_write_failed": ErrorRemedy(
        meaning=(
            "The write raised before it confirmed, so how much of the stimulus reached the line is unknown. Some of it "
            "may have arrived and been acted on, or none of it. `retry_safe` is false for that reason, and the write "
            "is recorded under `cleanup_reasons` as `com_write_effect_unconfirmed`. `backend_error` and "
            "`likely_causes` say what the driver reported."
        ),
        remediation=(
            "Call `com_read` first: what the target answered, or did not, is the best evidence of how much of the "
            "stimulus it received.",
            "Bring the target to a known state before the next stimulus that depends on its state, by its own "
            "protocol or controls, or with `reset_target` where the configuration allows it.",
            "Read `quarantine_guidance` for what the failed write leaves unconfirmed.",
        ),
        do_not=(
            "Do not send the same stimulus again as if nothing went out. Part of it may already be on the target, and "
            "a second copy can apply a command twice.",
        ),
    ),
    "serial_write_incomplete": ErrorRemedy(
        meaning=(
            "The line took only part of the payload: `bytes_written` of `bytes_requested` reached it, and `data` shows "
            "exactly which bytes. The rest never left the host. On Linux, with `write_timeout_s` above 0, a write hands "
            "the line only what it carries in that time at its baudrate and framing and does not send the rest; "
            "otherwise the remainder was retried a bounded number of times first. This "
            "is confirmed rather than unknown, so the session stays open and usable, and the short write is recorded "
            "under `cleanup_reasons` without holding the port. `likely_causes` names the usual reasons: a "
            "`write_timeout_s` too short for the payload at this baudrate, flow control, or a disconnect partway."
        ),
        remediation=(
            "Call `com_read` to see how the target took the partial command.",
            "Then send only what is missing, the bytes from offset `bytes_written` on, where the protocol accepts a "
            "command arriving in two pieces. Otherwise bring the target to a known state and send the command as a "
            "new one.",
            "If short writes keep happening, have the operator raise `write_timeout_s` for this port, or check flow "
            "control and the cable.",
        ),
        do_not=(
            "Do not repeat the whole payload. Its first `bytes_written` bytes are already on the target, and sending "
            "them again hands it those bytes twice.",
        ),
    ),
    "com_buffer_clear_failed": ErrorRemedy(
        meaning=(
            "`clear_buffer` asked for the port's receive buffers to be emptied and clearing them failed, so whether "
            "old input is still queued is unknown. A session that was already active stays open and usable, and the "
            "refusal carries no `cleanup_confirmed`. A session this call had just opened was closed again instead, "
            "and `cleanup_confirmed` is true. Where the clear had already reached the driver, it is recorded under "
            "`cleanup_reasons` as `com_buffer_clear_unconfirmed`. `backend_error` is the driver's line."
        ),
        remediation=(
            "Read `cleanup_confirmed`: true means the new session was closed again, so call `com_session_start` with "
            "`clear_buffer` again once the driver accepts the clear.",
            "Without it, the session that was already active is still open: call `com_read` once and discard what it "
            "returns, so that input the reader collected before the failed clear is not taken for an answer.",
        ),
        do_not=(
            "Do not take the first reply after this for a fresh answer to the next stimulus. Input queued before the "
            "failed clear may still arrive with it.",
        ),
    ),
    "session_not_active": ErrorRemedy(
        meaning=(
            "The tool needs a session that is not running, so nothing was sent and nothing was read. A COM call names "
            "its port in `port_id`, a CAN call its bus in `bus_id`, and a debug call refers to the one debug session. "
            "A COM session is not running when it was never started, when it was stopped, or when its reader failed; "
            "in that last case `reader_error` carries the reader's own error. A debug session is not running when it "
            "was stopped, ended in an error, or its GDB process exited."
        ),
        remediation=(
            "For a COM port, call `com_session_start` with the same `port_id`, then repeat the call.",
            "For a CAN bus, call `can_session_start` with the same `bus_id`, and on a bus with `shares` the same "
            "`participant`, then repeat the call.",
            "For a debug session, call `debug_start_session`. A debug session that ended in an error, or whose GDB "
            "process exited, has to be stopped with `debug_stop_session` before `debug_start_session` accepts a new "
            "one.",
            "When a COM refusal carries `reader_error`, read it first: the reader failed, and `com_session_start` with "
            "the same `port_id` replaces the failed session with a new one.",
        ),
        do_not=(
            "Do not retry the call in a loop hoping the session comes back. Nothing restarts a session but its start "
            "tool.",
            "Do not reach the device directly instead, with a serial terminal, a CAN tool or GDB of your own. That "
            "bypasses the lock, the permissions and the log.",
        ),
    ),
    "serial_read_failed": ErrorRemedy(
        meaning=(
            "The background reader of a COM session failed: the driver raised while reading, and `reader_error` "
            "carries its `backend_error` and `likely_causes`. The session is no longer active. Input the reader had "
            "already buffered is not lost: the next `com_read` still hands it out, with `reader_error` beside it, and "
            "only an empty buffer is refused as `session_not_active`. The COM tools carry this type nested under "
            "`reader_error`. `flash_firmware` with a `capture` answers it as its own `error_type` when the capture's "
            "reader failed after a good flash: the firmware was flashed and the target reset, the capture session was "
            "stopped, and `capture` holds what was read before the failure."
        ),
        remediation=(
            "Read `likely_causes` and `backend_error` in `reader_error`. A port that was disconnected is the first of "
            "them, and the adapter has to be back before a new session can open.",
            "Call `com_read` to collect what the reader buffered before it failed; those bytes were received from the "
            "target.",
            "Then call `com_session_start` with the same `port_id`: it replaces the failed session with a new one.",
        ),
        do_not=(
            "Do not keep calling `com_read` or `com_write` on the failed session in the hope it recovers. A failed "
            "reader does not restart; only `com_session_start` opens the port again.",
        ),
    ),
    "audit_write_failed": ErrorRemedy(
        meaning=(
            "The background reader received bytes it could not append to the session's COM log, so the evidence of "
            "what the target sent is incomplete. The session stopped, and the project is quarantined with the cleanup "
            "reason `com_reader_audit_broken`: while that incident stands, hardware calls are refused as "
            "`resource_quarantined`. This error is shown in `com_ports_list`, under the port's `reader_error`, with "
            "`backend_error` saying why the log write failed. `flash_firmware` with a `capture` answers it as its own "
            "`error_type` when this happened to the capture's reader after the flash, and `capture` holds what was "
            "read. `com_session_stop` closes such a session and answers `com_port_close_failed` with `quarantined` "
            "true; the port's lease stays with the incident, and the incident stands until an operator recovers it, "
            "which this server picks up while it runs."
        ),
        remediation=(
            "Fix what `backend_error` names first: free disk space, or restore write permission where `log_path` "
            "points, since every later record goes to the same place.",
            "Then an operator reads `quarantine_guidance`, checks the board against it, and runs `agentic-hil recover "
            "--confirm-safe-state --quarantine-id <quarantine_id>` with the id `agentic-hil lease-status` reports.",
        ),
        do_not=(
            "Do not delete or truncate the log to make room. It holds the record of what the target sent up to the "
            "failure.",
            "Do not sign `--confirm-safe-state` before the log is writable again. The next record would fail the "
            "same way.",
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
            "under `configured_com_ports`, `configured_can_buses` or `configured_debuggers`.\n\n"
            "It is never about a permission. A step the configuration's permissions deny is a valid plan and a bench "
            "that says no, and it is refused as `permission_denied` with the key it is about, on this surface exactly "
            "as over MCP. Nothing here applies to one, and the regeneration named below least of all."
        ),
        remediation=(
            READ_VALIDATION_NEXT_STEP,
            "A step naming a device the configuration does not declare is corrected in the plan: `device:` has to be "
            "one of the names the refusal lists under the `configured_*` key for that kind.",
            "A declared entry with no hardware behind it is filled in rather than argued with. With the board "
            "attached, `project_config_adopt_hardware` fills a declared debugger or COM-port entry's hardware in; "
            "adoption has no CAN half, so a `can_buses` entry's adapter and channel are written by hand.",
            "Only where the section is empty because `agentic-hil init` ran with no bench attached does the file need "
            "writing again: `agentic-hil init --force` from the project root writes it from the project profile and "
            "the hardware it finds. That is the missing-entry case and no other. `--force` replaces the whole file, "
            "every narrowed permission included, so it is a reset rather than a repair, and it is never the way past "
            "a permission: a refusal that names one is `permission_denied` and `{grant_command} <key>` moves that one "
            "key and leaves the rest of the file standing.",
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
            "Do not reach for `{reopen_command}` because a step was refused. It is the answer to an empty section and "
            "to nothing else: it rewrites the file from hardware discovery, so every narrowed permission, the "
            "baudrate, the `resource_id`, the `state_root` and the artifact roots go with it. A bench somebody "
            "narrowed on purpose is exactly the bench where that costs the most.",
        ),
    ),
    # The `test_config_invalid` the loader raises before it has a document: the
    # file is not UTF-8 text, it is not valid YAML or JSON, or its root is not a
    # mapping. The unscoped entry above is about a plan the loader has read, and
    # every step of it (device names, adoption, `init --force`, plan versions)
    # is about contents this refusal never saw; it sent a reader whose plan has
    # a duplicate key at a named line and column to reconfigure the bench
    # (#448's class, #504). Scoped on the field the three refusals carry, which
    # is the validator's own spelling of the whole document.
    f"test_config_invalid:{WHOLE_PLAN_FIELD}": ErrorRemedy(
        meaning=(
            "The test plan could not be read as a document, so nothing in it was checked and nothing on the bench "
            "was reached: it is not UTF-8 text, it is not valid YAML or JSON, or its root is not a mapping. `summary` "
            "says which. Where the parser could name the place, `line` and `column` locate the fault, counted from 1, "
            "and `backend_error` is the parser's own words for it. A duplicate key is refused here on purpose: a plan "
            "that names the same key twice would be run under whichever value the parser kept, which is not what "
            "anybody wrote."
        ),
        remediation=(
            "Open the plan at the `line` and `column` the refusal names and correct the document there; "
            "`backend_error` says what the parser found. Then run the plan again: nothing needs recovering, because "
            "no session was opened and no board was driven.",
            "A plan that is not UTF-8 was saved in another encoding; save it as UTF-8. A root that is not a mapping "
            "is a file whose top level is a list or a bare value, and a plan is a mapping with `version:` and "
            "`steps:` at the top.",
            "`{test_plan_reference}` shows the shape a plan takes once it parses; the schema is checked only after "
            "this refusal is cleared, so a step or key fault is reported on the next run by its own path.",
        ),
        do_not=(
            "Do not touch the bench configuration for this. Nothing here is about a device, a permission or an "
            "entry: the fault is in the plan's own text, and nothing written to the configuration changes what the "
            "parser reads there.",
        ),
    ),
    # The one `test_config_invalid` that is not about the plan's contents at all.
    # It is raised by the loader before a single byte of the file is read, so
    # every step the unscoped entry above offers is about a document nothing has
    # looked at: it sent a reader who typed one path to check device names, run
    # adoption and compare plan versions. This entry is scoped on the field the
    # refusal names so that the reader gets the move that fixes it and nothing
    # else.
    "test_config_invalid:workspace_root": ErrorRemedy(
        meaning=(
            "The plan path resolves outside the workspace this configuration binds. `path` is where it resolved to, "
            "symlinks followed, and `workspace_root` is the boundary it has to be inside. The file was never opened: "
            "nothing was parsed, nothing was locked and no hardware was reached, so this says nothing about whether "
            "the plan itself is valid.\n\n"
            "A relative path is resolved against `workspace_root` rather than against the process's working "
            "directory, which is what makes one plan path mean the same thing to the tool, to the command line and to "
            "a detached worker. A path that leaves the root, `..` and a symlink out of it included, is refused here."
        ),
        remediation=(
            "Move the plan inside the workspace root this refusal names under `workspace_root` and run it again by a "
            "path that resolves there; `next_step` names that root. Nothing else about the bench is involved and "
            "nothing needs recovering: the file was not opened.",
        ),
        do_not=(
            "Do not repoint `workspace_root` at the plan. That key is what this configuration authorizes, and widening "
            "it to admit one file admits everything else under the new root to every plan this bench runs.",
        ),
    ),
    "run_already_active": ErrorRemedy(
        meaning=(
            "This owner already holds an open run, and a run declares its devices once, up front. A test plan run through "
            "`test_reactor_run` is a run of its own and takes the devices it names for itself, so it is refused the same way "
            "while this session's `bench_run_start` is open, rather than as `device_busy` against this session's own hold."
        ),
        remediation=(
            "End the open run before declaring another; the devices it declared are released then.",
            "One run per owner is what makes the declared set the complete answer to what this owner may touch.",
            "For a plan, that is `bench_run_stop` and then `test_reactor_run` again; the plan needs no run around it.",
        ),
    ),
    "run_state_unwritable": ErrorRemedy(
        meaning=(
            "The runs directory under `state_root` refused a write, so the run was refused before it took any device. A "
            "run's record is what its handle names: without it nobody can watch the run or ask it to stop by name, and a "
            "detached worker would have run the whole plan behind a start command that reported it never came up. "
            "`runs_directory` names the directory and `errno` says what the operating system answered; nothing was "
            "locked or driven."
        ),
        remediation=(
            "Make the directory named in `runs_directory` writable for the user this command or server runs as, then "
            "start the run again; the retry is safe.",
            "A state root that is unwritable as a whole is refused when the configuration loads, as "
            "`unsafe_configured_path` on `state_root`. This refusal is the runs directory alone, which is usually a "
            "permission or ownership change made after the state root was created, or a disk that is full.",
        ),
        do_not=(
            "Do not delete the coordination state to get past this: the records beside the one that could not be "
            "written belong to runs that may still be going, and the leases and the audit trail under the same root "
            "are what `agentic-hil lease-status` and `agentic-hil recover` read.",
        ),
    ),
    # A plan run's own lifecycle: the handle a detached start prints, the
    # record behind it, and the ways a run ends that are not a verdict on the
    # firmware.
    "run_not_found": ErrorRemedy(
        meaning=(
            "This bench has no record under the handle in `run`, so nothing was asked of any run. The handle may be "
            "mistyped, may belong to another bench or another `state_root`, may be older than the 100 newest ended "
            "runs a bench keeps records of, or may be from a start that never published a record."
        ),
        remediation=(
            "Call `test_reactor_status` with no `run`: it lists every handle this bench still has a record of, "
            "newest first.",
            "For a run that ended long ago, its report is still where its result said; `get_last_report` reads the "
            "newest one.",
        ),
        do_not=(
            "Do not take a missing record as proof the run is gone and start the plan again on that basis. Check "
            "`hardware_lease_status` first: a run another bench or root started can still hold these devices.",
        ),
    ),
    "run_state_invalid": ErrorRemedy(
        meaning=(
            "A run record, or the directory that holds them, could not be read, so this bench cannot say what the "
            "run is doing. The run itself is not judged by this: it may be going exactly as asked. "
            "`backend_error` (and `record_error` with `record_path` on a detached start) says why; a record written "
            "by another version of Agentic HIL is refused the same way, with the summary saying so and no "
            "`backend_error`. A stop by name is refused for the same record, because it reads the record first."
        ),
        remediation=(
            "Fix what `backend_error` or `record_error` names (a permission, a full disk, a file held open "
            "elsewhere) and ask again; the retry is safe.",
            "A record from another version is read by the version that started the run; or wait for the run's own "
            "report.",
            "`hardware_lease_status` says whether the run still holds devices while its record cannot be read.",
        ),
        do_not=(
            "Do not delete or rewrite the record. It is the only thing naming that run, and a run whose record is "
            "gone can no longer be watched or stopped by name.",
            "Do not start the plan again on the assumption that the run ended.",
        ),
    ),
    "run_worker_failed": ErrorRemedy(
        meaning=(
            "The detached run's worker process ended before it published a record, so no run exists under the "
            "handle and nothing was locked or driven. `exit_code` is how it ended and `worker_output` is what it "
            "printed."
        ),
        remediation=(
            "Read `worker_output` and `exit_code`: they usually hold the refusal the worker met.",
            "Run the same plan without detaching to get that refusal as a result of its own, fix what it names, then "
            "start again; the retry is safe.",
        ),
        do_not=(
            "Do not restart the detached run unchanged in a loop. The worker will end the same way until the cause "
            "in `worker_output` is fixed.",
            "Do not delete the runs directory to clear this. Other runs' records live there.",
        ),
    ),
    "run_worker_unresponsive": ErrorRemedy(
        meaning=(
            "The detached run's worker did not publish a record within the startup window, and it may still be "
            "alive. A cooperative stop was planted under the handle in `run` before this answer, so a worker that "
            "does come up ends at its first step boundary instead of running the plan. `retry_safe` is false: the "
            "worker's state is unknown, and it may still be taking the devices."
        ),
        remediation=(
            "Ask `test_reactor_status` for this `run` a little later. A worker that came up late shows as stopped; "
            "`run_not_found` means it never registered.",
            "Call `hardware_lease_status` to see whether anything still holds the plan's devices, and start the "
            "plan again only once they are free.",
            "`worker_output` shows how far the worker got.",
        ),
        do_not=(
            "Do not start the plan again at once or in a loop. A late worker and a new one would queue for the same "
            "devices.",
            "Do not delete the planted stop: it is what keeps a late worker from driving the board behind a start "
            "that reported failure.",
        ),
    ),
    "run_worker_gone": ErrorRemedy(
        meaning=(
            "The process that was running this plan is gone and left no orderly end, so there is nobody to stop and "
            "the run has no report and no verdict of its own. The bench is the dead-owner case the coordinator "
            "handles."
        ),
        remediation=(
            "Call `hardware_lease_status`: it reads and heals the dead owner's holds, and names a `quarantine_id` "
            "if the run had reached the board.",
            "With an incident standing, recover it the way `resource_quarantined` describes; then start the plan "
            "again for a verdict.",
        ),
        do_not=(
            "Do not read the missing report as a pass or a failure. The run has no verdict.",
            "Do not delete the run record or the lock files, and do not send another stop; nothing is there to "
            "honour it.",
        ),
    ),
    "run_stopped": ErrorRemedy(
        meaning=(
            "The run ended on a stop request, which may be one somebody asked for by its handle or the stop a "
            "detached start planted after a worker that did not answer in time. It is neither a pass nor a failure "
            "of the firmware. `stopped_after_step` is the last top-level step that ran (0 when the stop came while "
            "the run was still waiting for a device, and then `resource` and `waited_s` say which device and how "
            "long). The steps that ran keep their records. The devices the run opened were closed the way a "
            "passing run closes them, and no recovery ran, because nothing was left unconfirmed."
        ),
        remediation=(
            "Read `stopped_after_step` and `steps` for what did run and what it showed.",
            "Start the plan again for a verdict on the whole plan; the retry is safe.",
            "A stop nobody here asked for came from another caller holding the handle, or from a stop a detached "
            "start planted.",
        ),
        do_not=(
            "Do not count the steps that never ran as passed.",
            "Do not report the plan as failed either: a stopped run has no verdict on the steps it did not reach.",
        ),
    ),
    "reactor_exception": ErrorRemedy(
        meaning=(
            "The test reactor raised outside any step, which is a defect in Agentic HIL rather than a verdict on the "
            "firmware. Every containment step was attempted, the report was written with no steps and with "
            "`cleanup` and `cleanup_ok` from the containment, and `exception_type` names what was raised. The run's "
            "own call answers with this failed report, and its handle carries the same error type."
        ),
        remediation=(
            "Read the report (`get_last_report`) for `cleanup` and `cleanup_ok`: they say whether the devices were "
            "closed.",
            "Call `hardware_lease_status` to see whether anything was left held or quarantined, and resolve that "
            "first.",
            "Report the defect with `exception_type` and the report.",
        ),
        do_not=(
            "Do not rerun the plan in a loop. The same defect raises the same way.",
            "Do not trust an older report as this run's. The report path is shared, and the run's own report is "
            "the one written now.",
        ),
    ),
    "interrupted": ErrorRemedy(
        meaning=(
            "The run was interrupted (Ctrl+C or a process exit) before it finished. Every containment step was "
            "attempted and the report was written with no steps and with `cleanup` and `cleanup_ok` from the "
            "containment. The run record behind its handle names this run `interrupted`."
        ),
        remediation=(
            "Read the report (`get_last_report`) for `cleanup` and `cleanup_ok`, then call `hardware_lease_status` "
            "to see whether anything was left held or quarantined.",
            "Start the plan again for a verdict.",
        ),
        do_not=(
            "Do not read the steps that did not run as passed.",
            "Do not delete lock files to free the bench. What the containment could not close is shown by "
            "`hardware_lease_status` and resolves through recovery.",
        ),
    ),
    "junit_xml_requires_synchronous_run": ErrorRemedy(
        meaning=(
            "A JUnit file was asked for beside a detached start. The file is written by the command that waits for "
            "the run's verdict, and a detached start returns before there is one, so the start was refused and no "
            "run began."
        ),
        remediation=(
            "Run the plan without `--detach` when a CI job needs the JUnit file; or detach without `--junit-xml` "
            "and follow the run with `test_reactor_status` and its JSON report.",
        ),
        do_not=(
            "Do not expect the detached worker to write the file later. Nothing writes it for a detached run.",
        ),
    ),
    "junit_xml_write_failed": ErrorRemedy(
        meaning=(
            "The run happened and its JSON report stands; only the JUnit file could not be written to `junit_xml`. "
            "A run that failed on its own keeps its own `error_type`, and the write failure is in `junit_xml_error` "
            "beside it, whose `backend_error` says what the write answered."
        ),
        remediation=(
            "Fix the path or the permission the `backend_error` in `junit_xml_error` names, then run the plan again "
            "with `--junit-xml` if the CI job needs the file.",
        ),
        do_not=(
            "Do not read the missing file as a test failure, or as a pass. The run's verdict is in its JSON report.",
        ),
    ),
    "cleanup_exception": ErrorRemedy(
        meaning=(
            "A cleanup action raised instead of answering: a device's close during a run's cleanup, or the reactor's "
            "or the service's own close after it. `exception_type` and `backend_error` say what was raised; `device` "
            "and `action` on the cleanup entry say which close. What that close left behind is unconfirmed."
        ),
        remediation=(
            "Call `hardware_lease_status`: `owner_active`, `device_holds` and `incident_stands` say whether anything "
            "was left held or quarantined, and an incident resolves the way `resource_quarantined` describes.",
            "Report the defect with `exception_type`, `backend_error`, `device` and `action`.",
        ),
        do_not=(
            "Do not delete lock files or coordination records to free what the close left.",
            "Do not rerun the plan at once. The next run meets the same unconfirmed state.",
        ),
    ),
    "cleanup_failed:test_reactor": ErrorRemedy(
        meaning=(
            "The plan run could not close everything it opened. `cleanup_errors` lists each close that failed, by "
            "`device`, `action` and its `result`. A step that had already failed keeps its own type in "
            "`step_error_type` beside `failed_step`, and a run asked to stop keeps `stopped`; the run's `error_type` "
            "says cleanup because a bench left in an unknown state outranks the verdict. When a device's close "
            "failed, a recovery of the probes the run drove was attempted and `recovery` says how it came out; a "
            "failed close of the reactor or the service after the run brings no `recovery` of its own."
        ),
        remediation=(
            "Read `cleanup_errors` for which device and which close failed, and what it answered.",
            "Read `recovery` where the run has one, then `hardware_lease_status`: with `incident_stands` true, "
            "resolve the incident the way `resource_quarantined` describes before the next run.",
            "Where `failed_step` is set, `step_error_type` is that step's own outcome; judge the firmware by it once "
            "the bench is settled.",
        ),
        do_not=(
            "Do not call `debug_stop_session`, `com_session_stop` or `can_session_stop` from another server to "
            "settle this. The run's sessions belonged to its own service, which has closed, and a retry with no new "
            "evidence leaves an unconfirmed state unconfirmed.",
            "Do not delete coordination records or lock files to free the bench.",
        ),
    ),
    # The reactor's verdicts on a step. Each is a firmware or plan outcome the
    # step's own record holds the evidence for, so the entries send the reader
    # to the step at `failed_step` rather than to the bench.
    "comparator_unmet": ErrorRemedy(
        meaning=(
            "A step read what it was told to read and the value did not satisfy the comparator. This is a verdict "
            "on the firmware or the plan, not a bench fault. The failing step's record (`steps` at `failed_step`, "
            "and inside a repeat block its `iterations`) holds `comparator` and what was seen: `received_tail` and "
            "`bytes_received` for a serial read, `frames_tail` and `frames_read` for a CAN read, `reading` and "
            "`captured_value` (and `masked_value` under a mask) for a symbol."
        ),
        remediation=(
            "Compare what was seen with `comparator`. Zero bytes or zero frames points at the line before the "
            "firmware: wiring, baud rate or bitrate, or a board that did not boot.",
            "Fix the firmware or the plan, whichever is wrong, and run the plan again.",
        ),
        do_not=(
            "Do not loosen the comparator or widen `timeout_s` until it passes without knowing why it failed.",
            "Do not call it a bench fault. The read worked; the value is the finding.",
        ),
    ),
    "symbol_size_mismatch": ErrorRemedy(
        meaning=(
            "A symbol read returned a different size than the plan declared, so the value was not compared. "
            "`expected_size_bytes` is what the plan said and `size_bytes` is what the read returned."
        ),
        remediation=(
            "Compare `expected_size_bytes` with `size_bytes`, then correct the plan or the firmware, whichever "
            "changed.",
        ),
        do_not=(
            "Do not drop `size_bytes` from the plan to get past this. It is the check that the plan and the image "
            "agree on what the symbol is.",
        ),
    ),
    "symbol_width_not_numeric": ErrorRemedy(
        meaning=(
            "A numeric comparator was applied to a symbol that is not a 1, 2, 4 or 8 byte integer "
            "(`integer_widths`), so there was no number to compare and nothing was judged."
        ),
        remediation=(
            "Point the comparator at a scalar the firmware keeps in one of `integer_widths`, or compare the bytes "
            "another way. Declaring `size_bytes` on the step makes the plan check refuse a width like this before "
            "the run starts.",
        ),
        do_not=(
            "Do not treat this as a failed assertion about the firmware. The comparison never happened.",
        ),
    ),
    "uart_expect_timeout": ErrorRemedy(
        meaning=(
            "A serial expect step did not see `expected_text` or `expected_pattern` within `timeout_s`. "
            "`received_tail` is the end of what did arrive (`received_tail_truncated` when more came before it), "
            "and `bytes_received` and `reads` say how much and how often."
        ),
        remediation=(
            "Read `received_tail`: output that is there but different is a firmware or plan finding; fix whichever "
            "is wrong.",
            "Zero `bytes_received` points at the port, the wiring, the baud rate or a board that did not boot.",
        ),
        do_not=(
            "Do not raise `timeout_s` or loosen the pattern until it passes without reading what arrived.",
        ),
    ),
    "unexpected_stop": ErrorRemedy(
        meaning=(
            "The target stopped, but not at the breakpoint the step was waiting for. `stop` is where and why it "
            "stopped and `expected_breakpoint_id` is the one the step named."
        ),
        remediation=(
            "Read `stop`: a fault, a different breakpoint or a halt from outside each say something different about "
            "the firmware or the plan. Fix that and run the plan again.",
        ),
        do_not=(
            "Do not add breakpoints or widen timeouts to get past the stop. Where the target stopped is the "
            "finding.",
        ),
    ),
    "breakpoint_cleanup_failed": ErrorRemedy(
        meaning=(
            "The target stopped, but the breakpoint the step set could not be cleared afterwards, so the step is "
            "not a pass, wherever the target stopped. `breakpoint_cleanup` holds what the clear answered; the run's "
            "`cleanup` shows how the debug session was closed."
        ),
        remediation=(
            "Read `breakpoint_cleanup` for why the clear failed, then `cleanup` and `hardware_lease_status` for the "
            "state the session was closed in.",
            "Run the plan again once the bench is settled.",
        ),
        do_not=(
            "Do not read the step as passed because the target stopped. A breakpoint left in the target changes "
            "what the next run sees.",
        ),
    ),
    "uart_session_not_owned": ErrorRemedy(
        meaning=(
            "The plan closed a serial session it had already closed, or never opened. Nothing was sent to the port "
            "and the session was not touched."
        ),
        remediation=(
            "Fix the order of the plan's open and close steps. A close inside a repeat block runs on every "
            "iteration, so a session opened once outside it is closed by the first and refused by the second.",
            "The run's own cleanup closes whatever it still holds, so nothing is left to close by hand.",
        ),
        do_not=(
            "Do not read this as a fault of the port or the board. It is the plan's order.",
        ),
    ),
    "can_session_not_owned": ErrorRemedy(
        meaning=(
            "The plan closed a CAN session it had already closed, or never opened. Nothing was sent on the bus and "
            "the session was not touched."
        ),
        remediation=(
            "Fix the order of the plan's open and close steps. A close inside a repeat block runs on every "
            "iteration, so a session opened once outside it is closed by the first and refused by the second.",
            "The run's own cleanup closes whatever it still holds, so nothing is left to close by hand.",
        ),
        do_not=(
            "Do not read this as a fault of the adapter, the bus or the board. It is the plan's order.",
        ),
    ),
    "step_exception": ErrorRemedy(
        meaning=(
            "A step raised instead of answering, which is a defect in Agentic HIL rather than a verdict on the "
            "firmware. Whether the step reached the board is unknown, so the run failed at that step and a "
            "recovery of the probes it drove was attempted (`recovery` says how it came out). `exception_type` and "
            "`backend_error` say what was raised."
        ),
        remediation=(
            "Call `hardware_lease_status` and resolve anything left standing.",
            "Report the defect with `exception_type` and `backend_error`, then run the plan again once.",
        ),
        do_not=(
            "Do not count it as a firmware verdict.",
            "Do not rerun the plan in a loop. The same defect raises the same way.",
        ),
    ),
    "preflight_exception": ErrorRemedy(
        meaning=(
            "The check that runs before the first step raised instead of answering, which is a defect in Agentic "
            "HIL. No step ran and nothing was driven. `validation_error` holds `exception_type` and "
            "`backend_error`, with `field` `$` because the check was about the whole plan."
        ),
        remediation=(
            "Report the defect with `exception_type`, `backend_error` and the plan that triggered it.",
        ),
        do_not=(
            "Do not rewrite the plan to dodge it. The plan was not judged, and a plan edited around a defect hides "
            "it from the next run.",
        ),
    ),
    "test_config_not_found": ErrorRemedy(
        meaning=(
            "There is no plan file at `path`, so nothing was parsed, locked or driven. With no path given, the run "
            "looks for `.agentic-hil/testconfig.yaml`, and a relative path is resolved against `workspace_root`, "
            "not against the shell's working directory."
        ),
        remediation=(
            "Pass the plan's path relative to the workspace root, or create the plan at "
            "`.agentic-hil/testconfig.yaml`.",
        ),
        do_not=(
            "Do not repoint `workspace_root` at the plan's directory. That key is what the configuration authorizes.",
        ),
    ),
    "test_config_unreadable": ErrorRemedy(
        meaning=(
            "The plan file at `path` exists but could not be read, so nothing was parsed, locked or driven. "
            "`backend_error` says what the read answered."
        ),
        remediation=(
            "Fix what `backend_error` names (usually a permission or a file another program holds open), then run "
            "the plan again.",
        ),
        do_not=(
            "Do not widen the permissions of the whole workspace to read one file.",
        ),
    ),
    "test_config_schema_invalid": ErrorRemedy(
        meaning=(
            "The plan schema bundled with this installation could not be used (`schema` names it, `schema_error` "
            "says why), so the plan was never checked and nothing ran. The plan is not the problem; the "
            "installation is."
        ),
        remediation=(
            "Repair the installation: `agentic-hil upgrade`, or reinstall Agentic HIL, then check it with "
            "`agentic-hil --version` and run the plan again.",
        ),
        do_not=(
            "Do not edit or loosen the plan to get past this. It was never read against the schema.",
            "Do not edit the schema inside the installation. The next upgrade replaces it, and until then every "
            "plan is checked against a schema nobody shipped.",
        ),
    ),
    "run_report_not_found": ErrorRemedy(
        meaning=(
            "`agentic-hil run-evidence --report` named a file that is not there (`path`), so no evidence was written. "
            "The report it reads is the JSON a test run produced: what `agentic-hil test-reactor --json` printed, "
            "saved to a file, or the run's own report file, which the run's result names in `canonical_report_path`."
        ),
        remediation=(
            "Check `path` against where the run's report actually went. A relative path is read from the directory "
            "`run-evidence` runs in, which in a CI job is not always the one the run step wrote from.",
            "If the run step wrote no file at all, read that step first: a run that never started, or output "
            "redirected to another name, leaves nothing here.",
            "Run `agentic-hil run-evidence` again with the path of the report the run wrote.",
        ),
        do_not=(
            "Do not write a report by hand, or copy one from another run, to give the command something to read. The "
            "evidence would describe a run that did not happen.",
        ),
    ),
    "run_report_unreadable": ErrorRemedy(
        meaning=(
            "The file `agentic-hil run-evidence --report` named (`path`) is there and could not be read as UTF-8 "
            "text, and `backend_error` says why: a permission, or bytes that are not UTF-8. No evidence was written."
        ),
        remediation=(
            "Read `backend_error`. A permission error means the account running `run-evidence` cannot read the file; "
            "give it read access and run the command again.",
            "A decode error means the file is not UTF-8. Windows PowerShell 5.1 writes UTF-16 with `>`: read the "
            "run's own report file (`canonical_report_path` on the run's result) instead, or save the output with "
            "PowerShell 7 or later, whose `>` writes UTF-8.",
            "Run `agentic-hil run-evidence` again.",
        ),
        do_not=(
            "Do not write or edit the report by hand to get past this. The evidence is worth what the run wrote and "
            "nothing more.",
        ),
    ),
    "run_report_invalid": ErrorRemedy(
        meaning=(
            "The file `agentic-hil run-evidence --report` named (`path`) is text and is not a JSON object: it does "
            "not parse as JSON (`backend_error` says where it stopped), or it parses as something other than an "
            "object. No evidence was written. A saved run report is the whole of what `agentic-hil test-reactor "
            "--json` printed to standard output, and nothing else."
        ),
        remediation=(
            "Look at the start of the file. A human-readable result means the run was saved without `--json`; text "
            "before the opening brace means standard error was mixed in, usually by `2>&1`; an empty file means the "
            "run printed nothing, and its own step log says why; a byte order mark at the very start fails the parse "
            "as well.",
            "Save the run's output again with `--json` and standard output alone, or read the run's own report file "
            "(`canonical_report_path` on the run's result) instead.",
            "Run `agentic-hil run-evidence` again.",
        ),
        do_not=(
            "Do not repair the report by hand until it parses. A document assembled to satisfy the parser describes "
            "a run that did not happen, and the evidence is worth what the run wrote and nothing more.",
        ),
    ),
    "audit_failed": ErrorRemedy(
        meaning=(
            "The action ran, but its evidence could not be written: `audit_ok` is false and `audit_error` says "
            "which write failed. A step that drove the board did so; what is missing is the record of it, and the "
            "bench may have been quarantined for it. `classify_last_error` answers the same type for the same case."
        ),
        remediation=(
            "Read `audit_error` and fix the write it names (a permission, a full disk, the audit ledger).",
            "Call `hardware_lease_status`: an incident the missing evidence raised stands until it is resolved the "
            "way `resource_quarantined` describes.",
        ),
        do_not=(
            "Do not run the step again just to get a clean record. The action already happened once, and repeating "
            "it changes the board again.",
        ),
    ),
    "step_failed": ErrorRemedy(
        meaning=(
            "A step's result failed one of the run's success checks without naming an error type of its own. Most "
            "often the call worked but giving its lease back did not (`lease_state`, `cleanup_required`, "
            "`quarantined`), or its `side_effect_status` or `hardware_state` is unknown. The run failed at that "
            "step and a recovery of the probes it drove was attempted (`recovery` says how it came out)."
        ),
        remediation=(
            "Read the step's record at `failed_step`: which success check it failed is in its own fields.",
            "Call `hardware_lease_status` and read the run's `recovery`; resolve anything left standing, then run "
            "the plan again.",
        ),
        do_not=(
            "Do not read a step's `ok: true` as a pass when the run names it `step_failed`. A call that left the "
            "bench in an unknown state is not a pass.",
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
    # Two `invalid_argument` refusals that are not about a tool payload at all,
    # scoped on the argument each names. The unscoped entry above explains
    # `field` and `validator`, `inputSchema` in `tools/list` and `wait_s: true`;
    # a permission name typed at `agentic-hil grant` and a run handle typed at
    # `agentic-hil test-reactor-status --run` carry no `validator` and were
    # decided against no schema, and the reader was sent through four steps and
    # three bullets about a surface they were not on before the one line they
    # could act on (#504).
    "invalid_argument:keys": ErrorRemedy(
        meaning=(
            "A name given to `agentic-hil grant` or `agentic-hil revoke` is not a permission key of this "
            "configuration. `rejected_keys` lists each one with why, and `permission_keys_here` lists every key the "
            "command accepts, read out of the file as it stands. Nothing was written: the command writes all of its "
            "names or none of them."
        ),
        remediation=(
            "Name one of `permission_keys_here`. A permission is named one at a time and several may be given in one "
            "command; there is no wildcard and no whole-entry form, so what is opened is exactly what was typed.",
            "A key is spelled as the section, the entry and the permission (`debuggers.dut.permissions.allow_flash`); "
            "the shorter `debuggers.dut.allow_flash` is accepted too. The entry names are this bench's own, which is "
            "why the list comes out of this file rather than out of a reference.",
            "`agentic-hil doctor` shows each configured entry with the permissions it grants today, if the question "
            "is what the bench allows rather than how a key is spelled.",
        ),
        do_not=(
            "Do not reach for `{reopen_command}` to make a name valid. It rewrites the whole file from the project "
            "profile, every narrowed permission included, and adds no permission key the schema does not already have.",
        ),
    ),
    "invalid_argument:run": ErrorRemedy(
        meaning=(
            "The value given as a run handle is not one. A handle is the `run` value `test_reactor_run` answered when "
            "the run started: `run-` followed by sixteen hexadecimal digits. `value` carries what was given. Nothing "
            "was reached: no record was read and no run was touched."
        ),
        remediation=(
            "Use the `run` value the start of the run printed, exactly as it was printed.",
            "Called without a handle, `test_reactor_status` lists every run this bench still has a record of, newest "
            "first, with the handle of each; that is where a handle nobody wrote down is found.",
        ),
        do_not=(
            "Do not guess a handle from a report file name or shorten one. The record is looked up by the exact "
            "handle, and a value that does not match the shape is refused before the lookup.",
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
    # The one error_type two unlike refusals share by name. Unscoped,
    # `config_file_not_found` is a workspace with no authoritative
    # configuration, and its steps write one. The ST-Link backend classifies
    # STM32CubeProgrammer's report of a file it could not open under the same
    # name, and its own summary says so ("Debugger input file could not be
    # found."): the configuration is there, or the call would never have reached
    # the programmer, and the path that is wrong is one this call passed.
    # Without this entry that refusal travelled with the other one's steps and
    # sent an operator whose firmware path was wrong to write a configuration
    # they already have (#506).
    "config_file_not_found:stlink": ErrorRemedy(
        meaning=(
            "STM32CubeProgrammer reported a file it could not open. It is a path this call passed, not a missing "
            "configuration: this workspace has an authoritative configuration, or the call would not have reached the "
            "programmer at all."
        ),
        remediation=(
            "Read `programmer_output` for the name the CLI printed. That is the file it could not open, and it is the "
            "only path this refusal is about.",
            "If it is the firmware, check the `image_path` this call passed. It is resolved under `workspace_root` "
            "before the programmer is started, so a file that was there when the call was accepted and gone when the "
            "programmer opened it (a build that reran, a clean, an artifact written to another directory) reads "
            "exactly like this.",
            "If it is not the firmware, it is a file the programmer went looking for itself: STM32CubeProgrammer reads "
            "device descriptions and external loaders out of its own installation, so a `debuggers.<name>.executable` "
            "copied out of that installation and run on its own reports them missing. Point the key at the installed "
            "programmer rather than at a copy of the binary.",
            "Report the printed path to the operator and ask which file was meant. Nothing about this workspace's "
            "configuration has to change to answer that.",
        ),
        do_not=(
            "Do not write or rewrite the authoritative configuration. `project_config_create` and `agentic-hil init` "
            "answer the other refusal that carries this name, the one about a workspace that has no configuration at "
            "all, and here they would overwrite a working one over a path in an argument.",
            "Do not retry the same path expecting another answer. The programmer looked for that file and did not "
            "find it, and a second attempt reaches the same absent file.",
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
            "Connect the probe. On Linux, install its udev rule and add this user to the group the rule names, then "
            "log in again; on Windows, bind the correct USB driver to it (ST-Link needs the ST driver, not WinUSB, "
            "unless the config selects a WinUSB interface).",
            "Set `debuggers.<name>.probe_id` to the serial number of the intended probe when more than one is attached; "
            "OpenOCD 0.12.0 and newer are passed it as `adapter serial`, older releases as the adapter driver's own "
            "serial command (`hla_serial` for `interface/stlink.cfg`).",
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
    # The probe enumerates and its USB link refuses every opener: recorded on the
    # reference board after an ST-LINK_gdbserver killed while it held the probe's
    # USB itself, in the direct stop round of
    # tests/fixtures/st_link_gdbserver_7_14_0_linux_session_stops_recordings.json,
    # with what gave the probe back measured in the same cycles.
    "adapter_usb_error:stlink": ErrorRemedy(
        meaning=(
            "The in-circuit debugger or programmer enumerates, but its USB link refused STM32CubeProgrammer's tools: "
            "ST-LINK_gdbserver printed `Target USB comms error`, STM32_Programmer_CLI `ST-LINK error (DEV_USB_COMM_ERR)`. "
            "It was recorded after an ST-LINK_gdbserver had been killed while it held the probe's USB itself."
        ),
        remediation=(
            "Call reset_target through an OpenOCD debugger entry for the same probe. In the recorded cycles that gave "
            "the probe back every time (8 of 8), although the reset itself answered `target_not_detected`; it resets "
            "the target, which then runs its firmware.",
            "An OpenOCD probe_target alone does not give it back: after one, ST-LINK_gdbserver answered "
            "`Target unknown error 19` and STM32_Programmer_CLI `ST-LINK error (DEV_TARGET_CMD_ERR)` until the reset.",
            "ST-LINK_gdbserver's own advice is to reconnect the probe's USB cable; that was not measured.",
            "To keep it from coming back, put stlink-server (STM32CubeCLT ships it) on PATH: debug sessions then reach "
            "the probe through it, and in the recorded shared round no session stop left the probe refusing.",
        ),
        do_not=(
            "Retry the same call unchanged: in every recorded cycle (8 of 8) the next server start and the next "
            "STM32_Programmer_CLI call were refused again until the reset.",
        ),
    ),
    # stlink-server could not open the probe for a GDB server reaching it through
    # stlink-server: recorded on the reference board when stlink-server had been
    # ended or restarted right after the previous session's GDB server was
    # killed, with what the next start met, in
    # tests/fixtures/st_link_gdbserver_7_14_0_linux_restarts_recordings.json and
    # tests/fixtures/st_link_gdbserver_7_14_0_linux_server_ends_recordings.json.
    "probe_server_open_failed:stlink": ErrorRemedy(
        meaning=(
            "stlink-server could not open the in-circuit debugger or programmer for the session's GDB server: its log "
            "says `TCPCMD OPEN_DEV FAIL`. ST-LINK_gdbserver words the same start as `Failed to connect to device`, "
            "the line a target that is off gives too, so the session reads stlink-server's log to tell them apart."
        ),
        remediation=(
            "Stop the session, which the refused start keeps for cleanup, and start it again. In the recorded rounds the "
            "next start came up after every such refusal (44 of 44), "
            "with nothing changed on the board.",
            "If it keeps coming back, look for a program that ends or restarts stlink-server while sessions use it. "
            "Ended at once after a session's GDB server was killed, stlink-server left the next start refused in "
            "6 of 40 recorded cycles; ended half a second later, or once it had released the probe's USB, in "
            "0 of 79. Sessions end it the second way.",
        ),
        do_not=(
            "Check the target's power and wiring first: the GDB server's line is the one a target that is off gives, "
            "and in the recorded refusals the next start came up with no change to either.",
        ),
    ),
    # `doctor`'s device-access check, which `init` repeats as a warning. It asks
    # the kernel with `os.access` and opens nothing, so these are the refusals the
    # first hardware call would meet, said before it is made.
    "device_access_denied:probe": ErrorRemedy(
        meaning=(
            "This account may not open the probe's USB device for reading and writing. `doctor` asked the kernel, "
            "which applies the node's mode, group and ACL to this process, and opened nothing. The first flash, reset "
            "or probe of the target would be refused as `adapter_not_found` with `backend_error_type: "
            "adapter_access_denied`. The check names the node, its owner, group and mode, whether this account is in "
            "that group in the account database, and whether this login holds it."
        ),
        remediation=(
            "Where `account_in_group` is true and `login_in_group` false, the account joined the group after this "
            "login began: log in again (a new SSH session or desktop login) and nothing else has to change.",
            "Otherwise an administrator adds this account to the group the check names, once (`sudo usermod -aG "
            "<group> <account>`; the probe's udev rule gives it to plugdev on Debian and Ubuntu), and the account "
            "logs in again.",
            "A node owned by root:root means no udev rule applies to the probe: install its rule (OpenOCD's "
            "60-openocd.rules, or the one ST ships with its tools), replug the probe, join the group the rule names "
            "and log in again.",
            "`ls -l` on the node shows its owner, group and mode; run `agentic-hil doctor` again after the change. "
            "TROUBLESHOOTING.md section 6 is the rest of it.",
        ),
        do_not=(
            "Do not run Agentic HIL, OpenOCD or the agent as root to get past this: the refusal is about this "
            "account, and a root process leaves root-owned files in the state root that the account cannot clean up.",
            "Do not chmod the node. udev creates it again at the next replug or boot, and a mode that admits this "
            "account admits every account on the machine.",
        ),
    ),
    "device_access_denied:com_port": ErrorRemedy(
        meaning=(
            "This account may not open the configured serial port's device node for reading and writing. `doctor` "
            "asked the kernel at the node the configured path resolves to, a /dev/serial/by-id link followed to its "
            "tty, and opened nothing. The first serial session on the port would be refused as "
            "`com_port_open_failed`. The check names the node, its owner, group and mode, whether this account is in "
            "that group in the account database, and whether this login holds it."
        ),
        remediation=(
            "Where `account_in_group` is true and `login_in_group` false, the account joined the group after this "
            "login began: log in again (a new SSH session or desktop login) and nothing else has to change.",
            "Otherwise an administrator adds this account to the group the check names, once (`sudo usermod -aG "
            "<group> <account>`; dialout on Debian and Ubuntu, uucp on Arch), and the account logs in again.",
            "A node owned by root:root means no udev rule applies to the adapter: install a udev rule for it, replug "
            "it, join the group the rule names and log in again.",
            "`ls -l` on the node shows its owner, group and mode; run `agentic-hil doctor` again after the change. "
            "TROUBLESHOOTING.md section 11 is the rest of it.",
        ),
        do_not=(
            "Do not run Agentic HIL or the agent as root to get past this: the refusal is about this account, and a "
            "root process leaves root-owned files in the state root that the account cannot clean up.",
            "Do not chmod the node. udev creates it again at the next replug or boot, and a mode that admits this "
            "account admits every account on the machine.",
        ),
    ),
    "probe_inventory_incomplete": ErrorRemedy(
        meaning=(
            "Bootstrap discovery found no probe to bind and cannot say the bench is empty. STM32CubeProgrammer is not "
            "installed, so probes are enumerated from this host's USB serial inventory, which reaches an ST-Link only "
            "through the virtual COM port a V2-1 or a V3 publishes, and that inventory showed no ST-Link at all. A "
            "standalone ST-LINK/V2, or any probe that exposes no VCP, can be attached and never appear there, so this "
            "reading is a blind spot rather than proof that nothing is connected: `adapter_not_found` would be a claim "
            "about hardware this enumeration cannot see. `project_config_create` writes nothing and `agentic-hil init` "
            "writes an unbound placeholder. It is only ever the empty reading: one visible ST-Link is bound and carries "
            "`probe_inventory: incomplete` into the result and the generated file, and two or more are "
            "`ambiguous_hardware`."
        ),
        remediation=(
            "Attach a probe that publishes a virtual COM port and run the discovery again: the one ST-Link this "
            "inventory then shows is bound on its own, with the incomplete count recorded on the entry it writes. "
            "`agentic-hil debugger-probes` and `agentic-hil com-ports` show what this host can currently see.",
            "Where the bench has a probe this inventory cannot reach, name the intended board's serial instead: "
            "`project_config_adopt_hardware` given it as `probe_id` (`--probe-id <serial>` at a shell) binds it, on a "
            "workspace that already has a configuration for adoption to fill.",
            "Or install STM32CubeProgrammer for an authoritative count: its own listing reads the serial off the probe "
            "directly rather than through a virtual COM port, so it sees a VCP-less ST-LINK/V2 the inventory cannot, "
            "then run the generation again.",
        ),
        do_not=(
            "Do not read this as `adapter_not_found` or an absent bench. An empty inventory here is a blind spot, not a "
            "proof that no probe is attached: a VCP-less ST-LINK/V2 could be plugged in right now, so reseating or "
            "re-attaching hardware that is already there is the wrong move.",
            "Do not read it as a rule against binding a single probe. One visible ST-Link is bound off this same "
            "inventory and the incomplete count travels with it; this refusal is about a reading with nothing in it to "
            "bind, and a second probe with no virtual COM port is what `--probe-id` is for.",
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
            "single-action run, that incident ends the moment the call ends, in one of two ways the result names. "
            "Where the run's reset into halt and re-probe confirm, its recovery settles the incident and the result "
            "carries `recovery.incident_resolved: true`; where the bench's policy or the probe's grants withhold that "
            "reset, or the reset or the re-probe does not confirm, the incident is stood down and the result carries "
            "`incident_stood_down`. Both endings return `quarantined: false`, the bench is handed back automatically, "
            "and the next flash is simply accepted. When the call ran inside a declared run (`bench_run_start` … "
            "`bench_run_stop`), the run owns the hold, so the failed result stays `quarantined: true` with no "
            "`incident_stood_down`, and the declared run keeps the probe until `bench_run_stop`, whose run teardown "
            "ends the incident in one of the same two ways and names which in its own result. Either way the incident "
            "owes nobody a signature, because it does not stand: `hardware_lease_status` reports "
            "`incident_stands: false`, including inside a declared run whose results still say `quarantined: true`, "
            "and that is what both recovery routes ask first, so `hardware_recover` and "
            "`recover --confirm-safe-state` both answer `nothing_to_recover: true`, and a retry is accepted without a "
            "recovery step rather than being refused with `resource_quarantined`. The signature is owed only for an "
            "incident that stands: a broken audit. It is not a free retry, "
            "though: a refused erase does not prove the flash is untouched, which the result still says, "
            "`cleanup_required` stays true and `cleanup_reasons` still names `debugger_result_unconfirmed`, so the "
            "board holds an indeterminate image until a retry programs and verifies.",
            "Read `programmer_output.stdout` before anything else. It is the programmer's own account of what it "
            "erased, wrote and verified, and it is what says which operation stopped.",
            "Treat the board as holding an indeterminate image whichever reading `erase_abort_point` gave, "
            "`erase_refused_effect_unconfirmed` no less than `flash_change_underway` or `abort_point_unreadable`, "
            "because none of them proves the flash is unchanged. Reflashing is the way through it, not a retry taken "
            "as proof the erase never happened; the reflash needs no recovery step ahead of it, a bare call's "
            "incident has already ended with its call, and a declared run's is one the reflash is allowed to run "
            "under, but read it as writing over an unknown image rather than a clean one.",
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
            "Treat the board as holding an indeterminate image until a later operation confirms its contents. "
            "This service does not perform or claim a separate flash readback. A refused erase does not prove the flash "
            "is unchanged, which is why the result says the contents are unconfirmed, and reflashing is the way through "
            "it rather than a retry taken as proof the erase never happened.",
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
    # -- The three buckets a flash or a read lands in when the tool named no more
    # specific operation, scoped per backend the way the erase entries above are.
    #
    # `verify_failed`, `flash_failed` and `memory_read_failed` had no entry at
    # all, so a refusal an operator meets on a failed verify, a failed flash or a
    # failed read named the bucket and handed over no next step. One generic
    # entry per bucket could not have been written: a failed verify under
    # STM32CubeProgrammer is a connect mode and a set of option bytes, and a
    # failed verify under pyOCD is an erase, a program and a flash algorithm out
    # of a CMSIS pack, and an entry that fitted both would name neither tool's
    # own options. So each entry is the tool's own account of that operation,
    # reached through the scoped lookup `remediation_fields` already performs,
    # and a bucket on a backend nobody has written for stays silent rather than
    # being handed another tool's advice under a generic name (#516).
    "verify_failed:stlink": ErrorRemedy(
        meaning=(
            "STM32CubeProgrammer wrote the image and then refused to confirm it. The verify is its own step, the `-v` "
            "this backend passes after `-w`, and it read the flash back and found it different from the file: "
            "`Error: Verify failed at address <address>` is the line that says so, and the whole transcript travels "
            "with the result under `programmer_output`.\n\n"
            "The write happened. This is not one of the refusals that promise the target was never touched: the board "
            "holds an image nothing has vouched for, and how much of it is the file that was flashed is exactly what "
            "the failed verify declined to say."
        ),
        remediation=(
            "Read `programmer_output.stdout` before anything else. It is the programmer's own account of what it "
            "erased, wrote and verified, and the address in `Error: Verify failed at address <address>` places the "
            "mismatch inside the image, which is what separates a write that never landed from one sector that would "
            "not take it.",
            "If the mismatch is at or near the start of the image, ask whether the core was running while the "
            "programmer wrote. STM32CubeProgrammer connects hot plug here unless it is told otherwise, and a core "
            "executing out of the flash being written corrupts the write rather than refusing it. Setting "
            "`debuggers.<name>.connect_mode` to `under_reset` (the CLI's `mode=UR`) holds the core in reset for the "
            "flash; `project_config_set` writes it, it needs the probe's reset line wired to the target's NRST, and a "
            "running server takes it up with `project_config_reload_description` or a restart.",
            "If the same addresses fail on every attempt, ask the device about protection rather than about the image. "
            "Read the option bytes with STM32CubeProgrammer yourself (`-ob displ`) and look for write protection, PCROP "
            "or a read-out protection level over the sectors the image covers: a protected sector that takes the write "
            "and keeps its old contents reads back as precisely this mismatch.",
            "If neither fits, check that the artifact is the one meant for this part, and that a `.bin` is being "
            "written at the address it was linked for. `debuggers.<name>.flash_address` is where a raw binary goes, and "
            "one written at the wrong base differs from the file from its first sector on.",
            "Treat the board as holding an indeterminate image until a flash programs and verifies. A refused verify "
            "does not say how much of the write landed, so reflashing is the way through it rather than a retry taken "
            "as proof that nothing changed.",
        ),
        do_not=(
            "Do not report the firmware as flashed because the download step printed its own success. The verify is the "
            "step that says the board holds this image, and it is the one that failed.",
            "Do not grant `allow_mass_erase` to force the image through. That permission makes this service refuse "
            "flashing outright, and it answers a question about one address by erasing the whole device.",
        ),
    ),
    "verify_failed:pyocd": ErrorRemedy(
        meaning=(
            "The captured pyOCD output says `Verify failed at <address>`. This service does not independently read "
            "flash back, so the transcript alone does not establish what pyOCD compared or how much of the image "
            "reached the device. Treat the image as indeterminate and keep the full transcript under `programmer_output`."
        ),
        remediation=(
            "Read `programmer_output.stdout` and `programmer_output.stderr` before anything else, and the log the "
            "result names by `log_path`. Confirm the `Verify failed at <address>` line is present and read the lines "
            "before it to see what pyOCD reported about the erase, program and target connection.",
            "Check that `debuggers.<name>.target_type` names this device. The CMSIS pack supplies pyOCD's target "
            "memory map and flash algorithm; a near neighbour can select the wrong address or page size. "
            "`agentic-hil doctor` reports the resolved target support, and MCP resource " + TARGET_SUPPORT_URI + " "
            "describes how to check it.",
            "Check the probe and link if the transcript points to a communication problem. `debugger_probes_list` "
            "reports which probe this host can enumerate, while the transcript shows whether the target connection "
            "failed before the reported verify line.",
            "Treat the board as holding an indeterminate image until a later operation confirms its contents. "
            "This service does not perform or claim a separate flash readback.",
        ),
        do_not=(
            "Do not read this as a refused erase. pyOCD names an erase it could not perform in its own words and this "
            "service classifies that as `flash_erase_failed`; inspect the captured output to see which operation it "
            "describes.",
            "Do not answer it with a chip erase (`pyocd erase --chip` or a `--erase chip` flash). That erases the whole "
            "device rather than the sectors the image covers, and the same reasoning is why this service refuses to "
            "flash at all once `allow_mass_erase` is granted.",
        ),
    ),
    "flash_failed:pyocd": ErrorRemedy(
        meaning=(
            "pyOCD's flash reported a failure other than an erase it named. The run is "
            "`pyocd flash --no-reset` over the artifact this call passed, with the configured target and probe on the "
            "command line. The whole transcript "
            "travels with the result under `programmer_output`.\n\n"
            "Nothing is confirmed about how much of the image reached the flash, so the board holds an indeterminate "
            "image rather than either the old one or the new one."
        ),
        remediation=(
            "Read `programmer_output.stdout` and `programmer_output.stderr` before anything else, and the log the "
            "result names by `log_path`. They are pyOCD's own account of the run, and the line before the failure is "
            "what places it: the probe opening, the connect, the image being loaded, or the programming itself.",
            "Check the image is one for this part and this address. `pyocd flash` takes the load address out of an ELF "
            "or a hex file, and out of `debuggers.<name>.flash_address` for a raw `.bin`, so a binary written at the "
            "wrong base fails the moment the address falls outside a region the target describes.",
            "Check `debuggers.<name>.target_type` names this device. pyOCD gets the memory map and the flash algorithm "
            "from the CMSIS pack behind that value, so a value that resolves to a near neighbour of this part programs "
            "at addresses and page sizes the device does not have. `agentic-hil doctor` reports what the configured "
            "value resolves to, in `debuggers.<name>.target_support`; provenance is in MCP resource "
            + TARGET_SUPPORT_URI
            + ".",
            "If the run fails part-way rather than at the first sector, put pyOCD's own clock option to it yourself. "
            "This server passes the target and the probe and no clock of its own, so the link runs at pyOCD's default "
            "of 1 MHz: `pyocd flash --target <target_type> --frequency 100k` against the same board says whether a "
            "slower SWD clock carries the image. Report what that run answered rather than changing the bench on the "
            "strength of it.",
            "Treat the board as holding an indeterminate image until a later operation confirms its contents. "
            "This service does not perform or claim a separate flash readback, so read a reflash as writing over an "
            "unknown image rather than a clean one.",
        ),
        do_not=(
            "Do not reach for `--erase chip` or `pyocd erase --chip` to get the image through. That erases the whole "
            "device rather than the sectors the image covers, and it answers a question about an address or a pack by "
            "destroying everything else on the part; the same reasoning is why this service refuses to flash at all "
            "once `allow_mass_erase` is granted.",
            "Do not read this as a failed reset. `--no-reset` is on the command this backend runs, so no reset was "
            "attempted in it, and a post-flash reset that fails is reported as `reset_failed` with the flash already "
            "committed.",
        ),
    ),
    "flash_failed:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD's `program` command did not finish the flash, and the failure is neither an erase it named nor a "
            "verify mismatch. This backend runs `init`, then `program` over the image with `verify`, and `reset` too "
            "when the call asked for one; a run that reported a failure stopped somewhere inside that command, with "
            "`** Programming Failed **` as OpenOCD's own line for it. The whole transcript travels with the result "
            "under `programmer_output`.\n\n"
            "How far it got is not claimed. `program` stops at the first step that fails, so the board holds an "
            "indeterminate image rather than either the old one or the new one."
        ),
        remediation=(
            "Read `programmer_output.stdout` and `programmer_output.stderr` before anything else, and the log the "
            "result names by `log_path`. They are OpenOCD's own account of what it opened, examined and wrote, and the "
            "line before `** Programming Failed **` is what places the failure.",
            "Read the line as the write failing. `program` prints `** Programming Failed **` only when its "
            "`flash write_image erase` step fails, after `init` and `reset init` succeeded and before any read-back "
            "(a failed read-back is `** Verify Failed **`, which is `verify_failed`), so the error lines just before "
            "it are that step's own account of what stopped it.",
            "Read the flash bank OpenOCD was working from. `flash info <bank>` names the driver, the base address and "
            "the sector map it chose, and a bank whose base or size is not this device's fails at the first write "
            "outside it while the connect and the examine both succeeded.",
            "Check `debuggers.<name>.target_cfg` is this part's script and not a near neighbour's. The bank comes from "
            "that script, so `target/stm32f4x.cfg` against a part from another family declares a flash the device does "
            "not have. `project_config_describe` reports the value this bench is running with.",
            "Treat the board as holding an indeterminate image until a flash programs and verifies, and read the "
            "reflash as writing over an unknown image rather than a clean one.",
        ),
        do_not=(
            "Do not reach for a device unlock command such as `stm32f2x unlock`, or for a chip erase, to force the "
            "image through. Those answer a question about a bank or an address with a mass erase of the whole part, "
            "which destroys more than the failed operation asked for; the same reasoning is why this service refuses "
            "to flash at all once `allow_mass_erase` is granted.",
            "Do not read this as a reset problem. OpenOCD warns about the reset on nearly every `reset halt`, and this "
            "failure is about a command that had already reached the flash.",
        ),
    ),
    "memory_read_failed:pyocd": ErrorRemedy(
        meaning=(
            "A read of the target's memory through pyOCD did not produce the bytes that were asked for. "
            "`debug_symbol_value` and `debug_dump_symbol_ihex` run `pyocd commander` with a `savemem` over an address "
            "and a size resolved out of the ELF `flash_firmware` put on the board, and the read is settled on that "
            "window rather than on the exit code: the commander reporting a failure and a run that exits 0 leaving no "
            "file holding exactly `size_bytes` are both this failure, because half a status word is a different number "
            "rather than a smaller one.\n\n"
            "A read writes nothing, but it attached a probe to a live core, so a run that stopped part-way through "
            "leaves the board's state unproven and the result says so. Two shapes are the exception and name themselves "
            "in their own summary: a temporary file this host would not create, and a temporary path pyOCD's command "
            "tokenizer could not carry. Neither reached the target."
        ),
        remediation=(
            "Read the summary on the result first. `The private file this read needs could not be created.` and `The "
            "private file this read needs cannot be named on pyOCD's command line.` are about this host's temporary "
            "directory rather than about the board, nothing on the bench answers them, and everything below is for a "
            "read that was actually sent.",
            "Confirm the probe is still there and still this bench's. `debugger_probes_list` says what this host "
            "enumerates, and a read taken after a flash on a probe a second session has since claimed fails at the "
            "connect rather than at the address.",
            "Ask what the core was doing. This read attaches to a running core on purpose and passes nothing that "
            "resets or halts it, because a read that halted the target would not measure what the firmware did, so a "
            "core that faulted, entered a low-power mode gating the debug clock, or lost debug access after the flash "
            "refuses the read while the probe itself is healthy. `reset_target` and a fresh read say whether the memory "
            "is readable when the core starts clean, and that is the operator's call to make: it destroys the very RAM "
            "the read was asked for.",
            "Check the range the read asked for. The result carries the `address` and the `size_bytes` resolved out of "
            "the ELF, and a symbol whose window crosses the end of a region this target describes, or sits in memory "
            "that is not powered or not mapped yet, is one pyOCD cannot take however healthy the link is. "
            "`debug_symbol_info` answers where the symbol lives without touching the board.",
            "If the run completed and left no bytes at all, read `debuggers.<name>.target_type` as the next suspect. "
            "The memory map pyOCD reads by comes from the CMSIS pack behind it, and a value that resolves to a near "
            "neighbour of this part answers a read outside this device's map with silence rather than with an error. "
            "`agentic-hil doctor` reports what the configured value resolves to, in "
            "`debuggers.<name>.target_support`.",
        ),
        do_not=(
            "Do not report the value as zero, or as whatever bytes did arrive. Nothing was read, and a partial window "
            "is a different number rather than a smaller one.",
            "Do not reach for `pyocd commander` or another debugger by hand to get the value anyway. That takes the "
            "probe out from under this bench's coordination while an incident over it may still be open, and it leaves "
            "the operator with no record of what ran.",
        ),
    ),
    # -- The paths around a hardware action -------------------------------------
    # The image a tool was handed, the record a report tool reads back, the
    # configuration adoption fills in, and the dispatcher every tool passes
    # through. Most of these refuse before anything reaches the board, and each
    # entry says whether it did, because that is the first thing a caller
    # deciding whether to call again has to know (#645).
    "artifact_not_found": ErrorRemedy(
        meaning=(
            "The firmware image this call named does not exist, so nothing was validated, staged or flashed. Three "
            "shapes answer with it: an `image_path` that names no file, resolved against the workspace root and not "
            "against the directory the server was started from; an `artifact_id` that names no upload in this "
            "project's upload directory; and an upload whose private staged copy could not be read back, which "
            "carries a `backend_error`."
        ),
        remediation=(
            "Check the path against the workspace root: a relative `image_path` is resolved there, so `build/app.elf` "
            "is the file under the project, wherever the server was started from.",
            "If the image has not been built yet, build it first, then call again with the path the build wrote.",
            "For an `artifact_id`, upload the image again and use the id that upload returns. Uploads are kept per "
            "project, so an id from another project names nothing here.",
            "If the result carries a `backend_error`, the upload's staged copy could not be read back: that is a fault "
            "of this host's temporary directory, not of the image, and an upload made once it is repaired works.",
        ),
        do_not=(
            "Do not create a placeholder file at the path to get past this. The format checks refuse it, and with "
            "them switched off it would be flashed.",
            "Do not substitute an image from another build or another project because it exists. The board would run "
            "firmware nobody asked for.",
        ),
    ),
    "artifact_changed": ErrorRemedy(
        meaning=(
            "The image changed between the moment it was validated and the moment it was staged for the backend, so "
            "nothing was sent to the board. Its content no longer matched the hash taken at validation, it was no "
            "longer a single-link regular file, it could not be opened (the result carries a `backend_error`), or a "
            "file appeared where validation found none, which only `validation.require_existing_file: false` "
            "permits. The result is `retry_safe: true`: calling again validates and stages the file afresh."
        ),
        remediation=(
            "Let the build that is writing the image finish.",
            "Call again once the file is stable; the new call validates what is there now.",
            "If the summary says the image did not exist when it was validated, build it first, then call again.",
            "If it repeats while nothing writes the file, read `backend_error`: the open itself is failing, which is "
            "a fault of the file or its directory that calling again does not change.",
        ),
        do_not=(
            "Do not flash the image with a raw programmer run to get around this. What would reach the board is an "
            "image nothing validated.",
            "Do not call again in a tight loop while a build is still writing the image.",
        ),
    ),
    "artifact_staging_failed": ErrorRemedy(
        meaning=(
            "The image passed validation and could not be copied into the private staging directory the backend "
            "reads it from. That directory lives in this host's temporary directory, is created when the server "
            "starts, and is neither in the workspace nor under `state_root`. `backend_error` names the failure; "
            "nothing was sent to the board (`side_effect_status: not_started`), so calling again is safe."
        ),
        remediation=(
            "Read `backend_error`: no space left, a write refused, or a read of the image itself failing part-way.",
            "Free space in, or restore write access to, this host's temporary directory, then call again.",
            "If the staging directory itself is gone (a temporary-directory cleaner removed it under a running "
            "server), restart the MCP server, which creates a new one.",
        ),
        do_not=(
            "Do not treat this as an incident to recover or sign for. Nothing reached the board and no lease was "
            "quarantined over it.",
            "Do not hand the image to the toolchain yourself because staging failed. Staging is what makes the bytes "
            "flashed the bytes validated.",
        ),
    ),
    "artifact_too_large": ErrorRemedy(
        meaning=(
            "The image is larger than this project accepts: `bytes` is its size, `max_bytes` the limit, set by "
            "`artifacts.max_upload_size_mb` in MiB. The limit applies to an image named by path, to an upload and to "
            "the staged copy alike, and it is checked before anything is sent to the board."
        ),
        remediation=(
            "Compare `bytes` with `max_bytes` and check this is the file meant: an ELF carries its debug information "
            "and can be many times the size of the image it programs.",
            "For a flash, use the `.hex` or `.bin` of the same build, which holds only what is programmed; a debug "
            "session needs the `.elf` and its symbols.",
            "If the image really is this large, the limit is the operator's to raise. No tool writes "
            "`artifacts.max_upload_size_mb`: it is edited in the configuration by hand and applies when the server "
            "restarts.",
        ),
        do_not=(
            "Do not truncate or split the image to fit. The board would be programmed with part of a firmware.",
            "Do not program the image with a raw programmer run instead.",
        ),
    ),
    "artifact_validation_failed": ErrorRemedy(
        meaning=(
            "The image was refused by the checks every image passes before it is flashed, and nothing was sent to the "
            "board. `validation` holds one flag per check: `path_traversal_safe`, `within_workspace` (which nothing "
            "relaxes), `allowed_root` (enforced under `validation.require_allowed_root`), `allowed_extension` "
            "(enforced under `validation.require_allowed_extension`), `regular_file` and `single_link`, and the "
            "format checks `elf_header`, `hex_parseable` and `bin_size_plausible`. A debug session also refuses an "
            "image that is not an `.elf`, because it needs the symbols."
        ),
        remediation=(
            "Read `validation` and the summary; the summary says which check stopped it.",
            "A format check that is false means the file is not the image its extension claims: rebuild it, or point "
            "at the build's real output. `regular_file` or `single_link` false means a link, a directory or a second "
            "hard link: use the plain file the build wrote.",
            "For a debug session, name the `.elf` of the build.",
            "For `within_workspace`, `allowed_root` or `allowed_extension`, move or build the image inside the "
            "workspace and under an allowed root, with an allowed extension.",
            "A refusal by policy that is wrong for this project is the operator's to change in the configuration.",
        ),
        do_not=(
            "Do not rename the file to an allowed extension. The format check reads the content, and an image that "
            "got past it under a false name would be programmed as something it is not.",
            "Do not edit `artifacts.allowed_roots`, `artifacts.allowed_extensions` or the `validation` switches "
            "yourself. They are operator policy, no tool writes them, and a change applies only when the server "
            "restarts.",
        ),
    ),
    "output_validation_failed": ErrorRemedy(
        meaning=(
            "The `output_path` of `debug_dump_symbol_ihex`, or of a dump step in a test plan, which is checked before "
            "the plan starts, was refused before anything was read from the board. It may not contain `..`, has to "
            "stay inside the workspace (nothing relaxes that), has to sit under an allowed artifact root when "
            "`validation.require_allowed_root` is on, and has to end in `.hex` or `.ihex`. `validation` holds the "
            "flag for each."
        ),
        remediation=(
            "Read `validation` to see which check refused the path.",
            "Name a path inside the workspace under an allowed root with a `.hex` or `.ihex` extension, such as "
            "`build/<symbol>.hex`.",
            "Call again with it; nothing was read, so nothing is lost.",
        ),
        do_not=(
            "Do not write the dump with another tool to put it where this refused it.",
            "Do not edit `artifacts.allowed_roots` yourself to admit the path. It is operator policy.",
        ),
    ),
    "audit_unavailable": ErrorRemedy(
        meaning=(
            "The call was refused before it started, because the bench could not record it. Every hardware action is "
            "written to an audit trail first (its report under `reports.directory`, its action log under "
            "`logs.directory`, the report state under `state_root`, a session's own log), and one of those could not "
            "be prepared. `audit_error`, where the result carries one, says what failed: a configuration refusal with "
            "its own `error_type`, or the exception class and its message. Nothing was flashed, reset or written to "
            "the target."
        ),
        remediation=(
            "Read `audit_error` for the path or the fault that stopped the record.",
            "Have the operator repair the destination: `agentic-hil doctor` checks that `state_root` accepts writes. "
            "Free the disk or restore write access. A destination this profile refuses outright is a configuration "
            "refusal, and the `audit_error` that names it carries its own remediation for that path.",
            "Call again once it is repaired. Nothing was started, so there is nothing to recover first.",
        ),
        do_not=(
            "Do not run the toolchain or a terminal program by hand to get the action done unrecorded.",
            "Do not delete report state or coordination records to make room. They are the record of what this "
            "bench has already done.",
        ),
    ),
    "audit_failed_after_action": ErrorRemedy(
        meaning=(
            "A hardware action ran and the record of it could not be written. Two paths end here. A hardware tool "
            "ended in a filesystem or configuration fault, which is read as a broken audit: `audit_error` and "
            "`backend_error` name it, and what the tool did to the board is unknown. Or "
            "`project_config_adopt_hardware` or `project_config_create` read the probe and could not write the record "
            "of that read or of its release; neither wrote anything to the configuration. Either way the bench is "
            "quarantined under an audit-broken reason, no automatic recovery clears that, and an operator signs it "
            "off."
        ),
        remediation=(
            "Fix where the record goes first: `agentic-hil doctor` checks that `state_root` accepts writes, and "
            "`audit_error` names the path or fault.",
            "Read what is known: `get_last_report`, and `hardware_lease_status` for the reasons on the incident and "
            "its `quarantine_guidance`.",
            "Have the operator check the board and sign the incident off with `agentic-hil recover "
            "--confirm-safe-state --quarantine-id <quarantine_id>`.",
            "Call `hardware_lease_status` again to confirm the bench is free, then repeat the action.",
        ),
        do_not=(
            "Do not repeat the action before the incident is settled. It is refused while the quarantine stands, and "
            "an action with no record is what the quarantine is there to stop.",
            "Do not delete reports, logs or coordination records to clear it.",
        ),
    ),
    "hardware_action_exception": ErrorRemedy(
        meaning=(
            "The hardware action raised part-way through and the service caught it. What it did to the board is not "
            "known: `side_effect_status: unknown`, `retry_safe: false`, and `backend_error` holds the exception. "
            "`quarantined` says whether an incident stands over the bench. When it is false the service already "
            "stood the incident down and the bench is free again; that settles the lock and says nothing about the "
            "board."
        ),
        remediation=(
            "Read `backend_error` for what raised.",
            "If `quarantined` is true, read `hardware_lease_status`: its `quarantine_guidance` and `auto_recoverable` "
            "say who settles the incident.",
            "Establish the state of the board before repeating the action: `get_last_report` holds what was "
            "recorded, and `probe_target` says whether the probe and the target still answer.",
            "Report an exception that repeats identically as a defect, with `backend_error`.",
        ),
        do_not=(
            "Do not call the same tool again in a loop. An action that raised once part-way and is repeated blind can "
            "leave the board half-done twice.",
            "Do not finish the action by hand with the toolchain.",
        ),
    ),
    "service_closed": ErrorRemedy(
        meaning=(
            "This service has shut down and takes no more calls. Over MCP that happens only while the server process "
            "is stopping. Nothing was started."
        ),
        remediation=(
            "Restart or reconnect the MCP server.",
            "Call `hardware_lease_status` on the new one before continuing, to see what the bench holds.",
        ),
        do_not=("Do not retry the call in a loop against this service. It does not reopen.",),
    ),
    "service_cleanup_required": ErrorRemedy(
        meaning=(
            "This service tried to shut down and the shutdown failed part-way: the backend, the artifact staging, a "
            "COM port or CAN session, or a child process would not close. It takes no more calls. What it could not "
            "close stays recorded as held, and the next server finds it as an incident."
        ),
        remediation=(
            "Restart the MCP server.",
            "Call `hardware_lease_status` on the new one: it names anything the old one left held.",
            "If that incident needs an operator's signature, read its `quarantine_guidance`, then have the operator "
            "sign with `agentic-hil recover --confirm-safe-state --quarantine-id <quarantine_id>`.",
        ),
        do_not=(
            "Do not delete coordination records or lock files to make the new server start clean.",
            "Do not keep calling this service; it does not reopen.",
        ),
    ),
    "unknown_tool": ErrorRemedy(
        meaning=(
            "This server has no tool by that name in its version. The name is checked before anything else, so "
            "nothing was started. A server with no configuration answers its own real tool names with "
            "`config_file_not_found` instead."
        ),
        remediation=(
            "List the tools with the MCP `tools/list` request and call one by its exact name.",
            "A command of the `agentic-hil` command line, such as `agentic-hil doctor`, is not a tool; run it in a "
            "shell.",
            "If the name comes from documentation for another release, compare versions: this server's is in the MCP "
            "`initialize` answer under `serverInfo`.",
        ),
        do_not=(
            "Do not reach for raw debugger, serial or shell commands because a tool name was not found.",
            "Do not guess near-miss names until one answers.",
        ),
    ),
    "report_not_found": ErrorRemedy(
        meaning=(
            "There is nothing to read yet. `get_last_report` answers this when no hardware call has written a report "
            "in this project; `classify_last_error` answers it when no failure is recorded, which includes every "
            "recorded call having succeeded. Reports are kept per project, by configuration file and workspace, under "
            "`state_root`."
        ),
        remediation=(
            "Make the hardware call whose report you want first, then read it.",
            "If a call was made and its report is not found, check this is the same server, configuration and "
            "workspace that made it: another project's reports are not read here.",
        ),
        do_not=(
            "Do not read this as a pass or a failure of anything. It says only that nothing is recorded.",
            "Do not create or edit files under `state_root` to give the reader something to find.",
        ),
    ),
    "report_unreadable": ErrorRemedy(
        meaning=(
            "This project's report state exists and reading it failed. `error_class` and `errno` say how; the path is "
            "withheld on purpose. A report state that reads and is damaged answers `config_invalid` instead."
        ),
        remediation=(
            "Read `error_class` and `errno`: a refused permission and a failing disk are different repairs.",
            "Have the operator restore access to `state_root`; `agentic-hil doctor` checks it.",
            "Until it is repaired, hardware calls meet the same fault and are refused as `audit_unavailable`.",
        ),
        do_not=(
            "Do not delete or recreate the report state to get past it. It is this project's record of what ran.",
            "Do not read this as an empty record or as a pass.",
        ),
    ),
    "report_write_failed": ErrorRemedy(
        meaning=(
            "Writing a report or audit record failed. `error_class` and `errno`, when present, identify the "
            "filesystem fault without exposing the state-root path. `backend_error` is what the write itself "
            "answered, and it is what tells one failed write from another when a call had more than one to make."
        ),
        remediation=(
            "Read `error_class` and `errno` for the fault and `backend_error` for the write that failed, then have "
            "the operator restore write access or free space at the report destination.",
            "Retry only after the report destination is writable; a hardware action whose audit failed may need "
            "the incident resolved before another action can run.",
        ),
        do_not=(
            "Do not delete or recreate report state to get past the write failure. It is this project's record of "
            "what ran.",
            "Do not repeat an action whose audit failed before resolving any incident it left behind.",
        ),
    ),
    "config_unreadable": ErrorRemedy(
        meaning=(
            "The configuration file exists and cannot be read: it is a directory or another non-regular file, the "
            "operating system refused or failed the read, or its bytes are not UTF-8. `path` and `backend_error` say "
            "which. Nothing was decided from it and nothing was written to it."
        ),
        remediation=(
            "Read `path` and `backend_error`.",
            "Have the operator fix the file where it is: restore read access, replace a directory with the file, or "
            "save it again as UTF-8.",
            "Call again once it reads; `agentic-hil doctor` reads it the same way and says when it does.",
        ),
        do_not=(
            "Do not regenerate the file with `{reopen_command}` to get past this. That resets every permission in it "
            "to the generated set and throws away what the operator decided.",
            "Do not delete or move the file aside, and do not point `AGENTIC_HIL_CONFIG` at another file to get "
            "around it.",
        ),
    ),
    "config_changed_underneath": ErrorRemedy(
        meaning=(
            "Another process wrote the configuration between the moment this call planned its change and the moment "
            "it would have written it, so nothing was written. `stale_keys` names each key whose `expected_value` "
            "and `current_value` now differ, or `document_changed: true` says the file changed elsewhere. The result "
            "is `retry_safe: true`."
        ),
        remediation=(
            "Re-read the configuration with `project_config_describe` to see what it says now.",
            "Plan again from what it says now. For adoption, call `project_config_adopt_hardware` again: it reads the "
            "file afresh and fills in only what is still unset.",
            "If it keeps happening, something else is writing the file; find it and ask the operator.",
        ),
        do_not=(
            "Do not force the values the stale plan carried (listed under `carried` in an adoption) into the file "
            "with `project_config_set`. They were planned against a file that no longer exists.",
            "Do not send the same plan again unchanged.",
        ),
    ),
    "unknown_device": ErrorRemedy(
        meaning=(
            "The call named a debugger, COM port or CAN bus id the configuration does not declare: an adoption "
            "`debugger_id`, a device of a `bench_run_start` run, or a device of a test plan step. It is looked up in "
            "the configuration, not on the bench. Nothing was held or started."
        ),
        remediation=(
            "Read `configured_debuggers` or `configured_devices` on the result: those are the ids this configuration "
            "declares.",
            "Call again with one of them, spelled exactly.",
            "If the device is attached and has no entry, read the configuration with `project_config_describe`. "
            "Adding an entry is the operator's decision: `project_config_set` writes one only under "
            "`permissions.allow_config_description_write`, and adoption never adds one.",
        ),
        do_not=(
            "Do not create an entry yourself to make the id exist.",
            "Do not substitute another declared id because it is the only one. It may be wired to another board.",
        ),
    ),
    "hardware_mismatch": ErrorRemedy(
        meaning=(
            "The configured debugger entry names one probe and the attached probe is another: "
            "`configured_probe_id` against `discovered_probe_id`. Adopting would describe two boards at once, so the "
            "whole plan was refused and nothing was written."
        ),
        remediation=(
            "Ask the operator which board this project is about, because each of the steps below answers a "
            "different one.",
            "If it is the configured board, attach it and call again.",
            "If another configured entry is meant for the attached board, call again with that entry's `debugger_id`.",
            "If the project moved to the attached board, the operator repoints `debuggers.<name>.probe_id` with "
            "`project_config_set` or in the file.",
        ),
        do_not=(
            "Do not clear or overwrite `probe_id` yourself to make adoption go through.",
            "Do not adopt into another entry because it has no `probe_id` yet.",
        ),
    ),
    "ambiguous_hardware": ErrorRemedy(
        meaning=(
            "More than one in-circuit debugger or programmer is attached, and discovery will not choose between "
            "them: choosing is how the wrong board gets configured. `probes` lists every attached serial. Nothing "
            "was read from a board and nothing was written."
        ),
        remediation=(
            "Ask the operator which board this project is about.",
            "Call `project_config_adopt_hardware` with its serial as `probe_id`.",
            "Or leave only that one connected and run the same discovery again.",
        ),
        do_not=(
            "Do not pick a serial from `probes` yourself.",
            "Do not read this as a fault to retry. The answer is the same until a board is named or unplugged.",
        ),
    ),
    f"not_supported:{UNBOUND_DEBUGGER_SCOPE}": ErrorRemedy(
        meaning=(
            "This tool drives one bound in-circuit debugger or programmer, and this configuration binds none: it "
            "declares no debugger at all, or it declares several and the server bound none of them. "
            "`configured_debuggers` lists what it declares. Calling again does not change that (`retry_safe: false`)."
        ),
        remediation=(
            "Read `configured_debuggers`.",
            "If it is empty, the configuration has no debugger: `project_config_create` generates one from the "
            "attached hardware (it needs `permissions.allow_config_write`), or the operator runs `{reopen_command}`.",
            "If it names several, drive them through `test_reactor_run` with a test plan that names the device of "
            "each step (`{test_plan_reference}`). Keeping only one entry is the operator's decision.",
        ),
        do_not=(
            "Do not retry the tool with other arguments. No argument binds a debugger.",
            "Do not delete or hand-edit debugger entries to leave one.",
        ),
    ),
    f"not_supported:{UNNAMED_PROBE_SCOPE}": ErrorRemedy(
        meaning=(
            "The bound debugger entry names no `probe_id` while other entries exist, so which attached probe it "
            "means is not settled. This is checked only when a tool is about to drive a board; nothing was started."
        ),
        remediation=(
            "Find the serial of this entry's probe: `debugger_probes_list` lists attached probes on a `pyocd` or "
            "`stlink` entry. OpenOCD cannot enumerate, so on an `openocd` entry read the serial off the probe or its "
            "USB listing.",
            "Write it with `project_config_adopt_hardware`, naming this entry as `debugger_id` and the serial as "
            "`probe_id` (it needs `permissions.allow_config_description_write`).",
            "Give every other entry its own `probe_id` the same way, so no two can mean one probe.",
            "Call the tool again.",
        ),
        do_not=(
            "Do not remove the other entries to make this one the only debugger.",
            "Do not guess a serial. A wrong one fails at the connect, and a right one for another board flashes it.",
        ),
    ),
    f"adapter_not_found:{DISCOVERY_SCOPE}": ErrorRemedy(
        meaning=(
            "Discovery found no in-circuit debugger or programmer to bind. Either the listing was authoritative and "
            "empty, or the host's USB serial inventory shows a probe's serial port and read no serial off it, or a "
            "`requested_probe_id` named a serial that is not among the attached ones in `probes`. Nothing was read "
            "from a board and nothing was written."
        ),
        remediation=(
            "If `requested_probe_id` is set, name one of the serials under `probes`, or attach the board with that "
            "serial: selection chooses among attached probes and never adds one.",
            "If the summary names a serial port with no serial behind it, check the probe is a genuine unit with its "
            "driver installed, or install STM32CubeProgrammer, which reads the serial off the probe itself.",
            "Otherwise nothing is attached: attach the probe with a data cable (not a charge-only one) and check its "
            "driver.",
            "Then bind it with `project_config_adopt_hardware`, or with `project_config_create` when there is no "
            "configuration yet.",
        ),
        do_not=("Do not write a `probe_id` that discovery did not list into the configuration.",),
    ),
    f"target_not_detected:{DISCOVERY_SCOPE}": ErrorRemedy(
        meaning=(
            "The in-circuit debugger or programmer `probe_id` answered, and its read-only hot-plug connect found no "
            "target behind it. Nothing was reset or written."
        ),
        remediation=(
            "Check the target is powered.",
            "Check the debug wiring between probe and target, the SWD lines and any jumpers that connect them, and "
            "that no other program holds the probe.",
            "Firmware that disables the debug pins also looks like this; recovering such a part is the operator's "
            "call, because discovery never connects under reset.",
            "Run the same discovery again.",
        ),
        do_not=(
            "Do not switch to another `probe_id` to get an answer.",
            "Do not force a connect under reset or an erase by hand.",
        ),
    ),
    f"debugger_not_executable:{DISCOVERY_SCOPE}": ErrorRemedy(
        meaning=(
            "The toolchain discovery found is present on this host and will not run. Discovery uses the "
            "STM32CubeProgrammer CLI when it resolves, and a broken install of it is refused here rather than "
            "falling back to OpenOCD; OpenOCD on `PATH` is used when the CLI is absent. Discovery reads no configured "
            "`executable`, and nothing was said to the board."
        ),
        remediation=(
            "Read `not_executable_reason`, and the path in `executable` or the summary.",
            "`permission_denied`: restore the execute bit with `chmod +x` on that path and check its filesystem is "
            "not mounted `noexec`.",
            "`not_an_executable_image`: `file <path>` says what it is instead; reinstall the toolchain for this "
            "machine.",
            "Run the same discovery again.",
        ),
        do_not=(
            "Do not set `debuggers.<name>.executable` to get past this. Discovery does not read it.",
            "Do not copy the toolchain into the workspace.",
        ),
    ),
    f"timeout:{DISCOVERY_SCOPE}": ErrorRemedy(
        meaning=(
            "Discovery started a read-only toolchain read and reaped it when it did not finish within discovery's own "
            "fixed deadline. The summary names which read: the STM32CubeProgrammer probe listing, or its hot-plug "
            "connect to the target, which change nothing on the board and report `hardware_state: unchanged`; or "
            "OpenOCD's init, targets and shutdown, whose `init` attaches to the target and can halt the core before "
            "the process was reaped, so it reports `hardware_state: unknown`. No configuration was generated or "
            "adopted from it."
        ),
        remediation=(
            "Read the summary and `hardware_state` on this result: they say which read was reaped and whether it can "
            "have left the core halted.",
            "If `hardware_state` is `unknown` and this result came under `hardware_discovery` in a "
            "`resource_quarantined` refusal, the board is held: follow that refusal's own `next_step` and remediation "
            "before anything else touches it. With no such refusal around it nothing holds the board, and the core "
            "may be sitting halted: have the operator reset or power-cycle the target before relying on it.",
            "Check that no other program holds the in-circuit debugger or programmer, that its cable is a data cable "
            "seated at both ends, and that the target is powered.",
            "Run the same discovery again: `project_config_adopt_hardware` on a workspace that has a configuration, "
            "otherwise the call or command that returned this.",
        ),
        do_not=(
            "Do not set `debuggers.<name>.timeout_s` to give discovery longer. Discovery runs with its own fixed "
            "deadline and reads no configured timeout.",
            "Do not switch to another `probe_id` to get an answer. Another probe answering says nothing about the one "
            "that timed out, and a configuration bound to it describes a different board.",
        ),
    ),
    "canonical_write_pending": ErrorRemedy(
        meaning=(
            "The report read back is a staged copy whose promotion to the canonical record failed (`audit_ok: false`, "
            "`canonical_write_pending: true`). Its `ok` is neither a confirmed success nor a confirmed failure: the "
            "run it describes is not recorded where the bench keeps its records."
        ),
        remediation=(
            "Treat the run as unconfirmed, whatever its `ok` says.",
            "Read `hardware_lease_status` for what the bench holds now.",
            "Have the operator repair the state root's filesystem; `agentic-hil doctor` checks it.",
            "Run the action again once records commit.",
        ),
        do_not=(
            "Do not report the run as passed on the strength of this copy.",
            "Do not edit or delete the staged report or the report state.",
        ),
    ),
    "cleanup_required": ErrorRemedy(
        meaning=(
            "`debug_stop_session` stopped the session's processes and the probe lease could not be handed back "
            "cleanly. `cleanup_reasons` says why, `lease_state` where the lease stands and `quarantined` whether an "
            "incident is open. The debug session is over; what is unsettled is the record of the lease, not the "
            "session."
        ),
        remediation=(
            "Read `cleanup_reasons`.",
            "If the only reason is `lease_release_unconfirmed`, the release record could not be written, which is a "
            "host-side fault the bench recovers by machine: call `debug_stop_session` again, which retries the "
            "release and answers that no session is active, without touching the board. Any later hardware call "
            "retries it too.",
            "If a reason ends in `audit_broken` (such as `debug_coordination_report_audit_broken`: the stop's report "
            "could not be written), calling again answers the same refusal. The operator repairs the audit "
            "destination (`agentic-hil doctor` checks it), checks the board and signs with `agentic-hil recover "
            "--confirm-safe-state --quarantine-id <quarantine_id>`.",
            "Read `hardware_lease_status` to confirm where the lease stands.",
        ),
        do_not=(
            "Do not delete coordination records to release the lease.",
            "Do not go after the session's processes by hand. They are already stopped.",
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
            "reads memory; it is not a debug server. Typed debug sessions on this backend run on ST-LINK_gdbserver, "
            "the GDB server STM32CubeCLT installs beside the CLI (#624), and this debugger has none: "
            "`debuggers.<name>.gdb_server_executable` is not set, and none was found beside the configured "
            "STM32_Programmer_CLI or elsewhere on the host when the configuration loaded. Without it there is no "
            "session on this backend to hold a breakpoint, resume a core, or report why one stopped, so "
            "`debug_start_session`, `debug_stop_session`, `debug_get_session_status`, `debug_set_breakpoint`, "
            "`debug_list_breakpoints`, `debug_clear_breakpoints`, `debug_continue`, `debug_halt` and "
            "`debug_get_stop_reason` are refused. `reset_target` with mode `init` is refused here whatever is "
            "configured, because its reset-init event script is an OpenOCD thing.\n\n"
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
            "a configuration change and not a different probe. The smaller one keeps this backend: install "
            "STM32CubeCLT, whose ST-LINK_gdbserver is found by itself beside its own STM32_Programmer_CLI, or name the "
            "server as `debuggers.<name>.gdb_server_executable`. Sessions then run through it with the GDB "
            "`debug.gdb_executable` names, and flashing, probing and reading are unchanged.",
            "The other keeps the probe and changes the stack: the same in-circuit debugger is one OpenOCD drives. Set "
            "`debuggers.<name>.type` to `openocd`, with `interface_cfg: interface/stlink.cfg` and the `target_cfg` for "
            "this part, `target/stm32f4x.cfg` for an STM32F4.",
            "Either is one `project_config_set` call behind `allow_config_description_write`: send "
            "`debuggers.<name>.gdb_server_executable`, or `debuggers.<name>.type` together with the fields the new "
            "backend requires, and it lands whole or is refused naming what is missing. Which debug stack a bench "
            "runs is the operator's decision, so report the change and get their word before making it. Afterwards "
            "the server adopts it through `project_config_reload_description` or a restart.",
            "Say what the move to OpenOCD costs before it is made, because parts of this bench change hands with it. "
            "OpenOCD has "
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
            "Do not reach for `openocd`, `gdb`, `ST-LINK_gdbserver`, `st-util` or a raw debugger command to get a "
            "breakpoint anyway. That bypasses the policy this refusal comes from, takes the probe out from under the "
            "bench's own coordination, and leaves the operator with no record of what ran.",
            "Do not swap the probe. The in-circuit debugger is not what refused; the configuration around it is, and "
            "the same probe serves both ways out.",
        ),
    ),
    "not_supported:pyocd": ErrorRemedy(
        meaning=(
            "This bench runs `type: pyocd`. The typed-debug family is served here: the session tools run through "
            "`pyocd gdbserver` with the GDB `debug.gdb_executable` names (#624), and `debug_symbol_value` and "
            "`debug_dump_symbol_ihex` also read the target with no session open, through pyOCD's own `savemem` on "
            "`--connect attach`, the one of pyOCD's connect modes that neither halts nor resets the core, with the target "
            "pack's DebugCoreStart sequence disabled so that the connect does not let a halted core run either.\n\n"
            "What pyOCD refuses is `reset_target` with mode `init`. On OpenOCD that mode halts the core and then runs "
            "the target's reset-init event script, which is where a board's clock tree, wait states and watchdog are "
            "set up; pyOCD's commander has no equivalent, and sending its plain `reset halt` under that name would "
            "report an initialised core that nobody initialised.\n\n"
            "Nothing was sent to the bench for this refusal. The target is exactly as the last call that did reach it "
            "left it."
        ),
        remediation=(
            "If stopping the core is what the step needs, use mode `halt`: on pyOCD it is `reset halt`, and the core "
            "stops at the reset vector. A typed debug session started with mode `reset_halt` does the same and keeps "
            "the core under GDB for a breakpoint, a resume or a stop reason.",
            "If the step genuinely needs the reset-init script, the way out is a configuration change rather than "
            "different hardware: the probe this backend is driving is one OpenOCD drives too. Set "
            "`debuggers.<name>.type` to `openocd`, with the `interface_cfg` for the probe that is actually plugged in "
            "(`interface/stlink.cfg` for an ST-Link, `interface/cmsis-dap.cfg` for a CMSIS-DAP probe) and the "
            "`target_cfg` for this part.",
            "The switch is one `project_config_set` call behind `allow_config_description_write`: send "
            "`debuggers.<name>.type` together with the fields the new backend requires, and it lands whole or is "
            "refused naming what is missing. Which debug stack a bench runs is the operator's decision, so report "
            "the change and get their word before making it. Afterwards the server adopts it through "
            "`project_config_reload_description` or a restart.",
            "Say what the move costs before it is made. OpenOCD has to be installed and reachable, by PATH or "
            "`debuggers.<name>.executable`. `debuggers.<name>.target_type` stops being read: OpenOCD is told what the "
            "part is by `target_cfg`, so the CMSIS device-family pack that made pyOCD resolve the part is no longer "
            "what decides whether the bench works, and a wrong `target_cfg` fails to detect the target rather than "
            "adapting.",
            "If the bench has to stay on pyOCD, report the step as unavailable on this configuration. A missing "
            "capability that is stated is a decision for the operator; one that is worked around quietly is a plan "
            "that reports something it did not do.",
        ),
        do_not=(
            "Do not send mode `halt` and report the core as initialised. The reset-init script did not run, and a "
            "step that needed it reads registers and memory a board that was never set up holds.",
            "Do not reach for `pyocd commander`, `pyocd gdbserver`, `gdb` or a raw debugger command to run an "
            "initialisation anyway. That bypasses the policy this refusal comes from and takes the probe out from "
            "under the bench's own coordination.",
            "Do not swap the probe. The probe is not what refused; the backend the configuration names for it is.",
        ),
    ),
    # Not a missing capability of the backend but of the installed OpenOCD: the
    # release decides whether the configured probe can be selected by serial.
    "not_supported:openocd_probe_selection": ErrorRemedy(
        meaning=(
            "This configuration names its probe by `probe_id`, and the installed OpenOCD has no way to select that "
            "probe by its serial. OpenOCD 0.12.0 and newer select every adapter driver's probe with `adapter serial`. "
            "Older releases, such as the 0.11 Ubuntu 22.04 packages, select one only through a command of the adapter "
            "driver's own: `hla_serial` for the hla driver `interface/stlink.cfg` loads, `st-link serial` for the "
            "st-link driver, `cmsis_dap_serial` for cmsis-dap. So the OpenOCD backend asks the installed OpenOCD which "
            "release it is and, before 0.12, which driver `interface_cfg` loads, both at OpenOCD's configuration stage "
            "where no adapter is opened, and uses that driver's command. `openocd_version` and `adapter_driver` on the "
            "result say what it was told: a driver with no command that takes this serial (`jlink serial` takes numbers "
            "only), or `undefined` for an interface script that loads no driver at all.\n\n"
            "The call was refused rather than sent without a selector, because without one OpenOCD opens whichever "
            "probe it finds first, and that need not be the board this configuration binds. Nothing was sent to the "
            "bench for this refusal. The target is exactly as the last call that did reach it left it."
        ),
        remediation=(
            "Read `openocd_version` and `adapter_driver` on the result: they are what the installed OpenOCD said about "
            "itself and about `debuggers.<name>.interface_cfg`. `debugger_info` reports the same release.",
            "Install OpenOCD 0.12.0 or newer, which selects every adapter driver's probe with `adapter serial`, and "
            "have `debuggers.<name>.executable` or PATH name it. Which OpenOCD a bench runs is the operator's "
            "decision, so report the refusal and get their word before changing it.",
            "If the probe is one an older OpenOCD can select by serial, an interface script for that driver does it on "
            "this release as well: `interface/stlink.cfg` (hla) or `interface/stlink-dap.cfg` (st-link) for an "
            "ST-Link, `interface/cmsis-dap.cfg` for a CMSIS-DAP probe. `debuggers.<name>.interface_cfg` changes "
            "through `project_config_set` behind `allow_config_description_write`, with the operator's word.",
            "Retry the call once the cause is fixed. The bench was not driven, so nothing has to be inspected or "
            "recovered first.",
        ),
        do_not=(
            "Do not remove `probe_id` to get past this refusal. Without a selector OpenOCD opens whichever probe it "
            "finds first, which is the wrong-board risk `probe_id` exists to rule out.",
            "Do not reach for `openocd` or a raw debugger command to select the probe by hand. That bypasses the "
            "policy this refusal comes from and takes the probe out from under the bench's own coordination.",
            "Do not inspect the hardware or run `agentic-hil recover` for this result. It is a refused call, not an "
            "unconfirmed target state.",
        ),
    ),
    # -- The debugger backends and the debug session, in their own words (#644) --
    "timeout:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD, or the GDB a debug session drives through it, did not answer before its deadline. The wait that "
            "ran out is one of these: OpenOCD's version check, an OpenOCD run for probe_target, flash_firmware or "
            "reset_target, the debug server's ready line at a session start (`backend_error_type` "
            "`gdb_server_not_ready`), or one GDB/MI command inside a session, a symbol lookup included. A process that "
            "runs out of time is stopped at the deadline, so the result knows about the board only what its state "
            "fields say."
        ),
        remediation=(
            "Read `target_state`, `side_effect_status`, `target_contacted` and `cleanup_required` first, where the "
            "result carries them. A version check reaches nothing, and a flash, a reset or a session command can have "
            "stopped partway.",
            "When `target_state` is `unknown` inside a debug session, call debug_halt: a confirmed halt settles the "
            "unconfirmed state, and the session goes on from the halted core.",
            "`gdb_server_not_ready` means OpenOCD did not print `Listening on port ... for gdb connections` in time. "
            "That line needs OpenOCD 0.11.0 or newer, so an older OpenOCD, or one configured to log elsewhere, times "
            "out here every time; the server output in the log at `log_path` says which.",
            "Every wait is bounded by `debuggers.<name>.timeout_s`, and a call's own `timeout_s`, where a tool takes "
            "one, only shortens it. A slow bench that needs a longer ceiling is the operator's edit of the "
            "authoritative file: project_config_set does not write that key.",
        ),
        do_not=(
            "Do not repeat an effectful call (flash_firmware, reset_target, debug_continue) before the state fields say "
            "where the one that timed out left the board. A second run over an unknown state adds a second unknown.",
            "Do not raise `timeout_s` in the call to wait longer. It can only shorten the configured ceiling.",
        ),
    ),
    "timeout:pyocd": ErrorRemedy(
        meaning=(
            "pyOCD, or the GDB this backend reads symbols out of the flashed ELF with or drives a debug session "
            "through, did not finish before its deadline: the version check, the probe listing, a run for "
            "probe_target, flash_firmware, reset_target or a memory read, a symbol lookup, the debug server's ready "
            "line at a session start (`backend_error_type` `gdb_server_not_ready`), or one GDB/MI command inside a "
            "session. A process that runs out of time is stopped at the deadline, so the result knows about the board "
            "only what its state fields say. The probe listing and the symbol lookup never contact the target."
        ),
        remediation=(
            "Read `side_effect_status`, `target_contacted` and `target_state` first, where the result carries them. A "
            "flash, a reset or a session command that timed out can have stopped partway, and the log at `log_path` "
            "holds what pyOCD printed before it was stopped.",
            "When `target_state` is `unknown` inside a debug session, call debug_halt: a confirmed halt settles the "
            "unconfirmed state, and the session goes on from the halted core.",
            "`gdb_server_not_ready` means `pyocd gdbserver` did not print `GDB server listening on port ...` for the "
            "session's port in time; the server output in the log at `log_path` says why.",
            "The deadline comes from `debuggers.<name>.timeout_s`, and a slow bench that needs more is the operator's "
            "edit of the authoritative file: project_config_set does not write that key. The probe listing already "
            "waits at least 30 seconds whatever the key says.",
        ),
        do_not=(
            "Do not repeat a flash, a reset or a memory read before the state fields say where the one that timed out "
            "left the board. A second run over an unknown state adds a second unknown.",
            "Do not run `pyocd` by hand to see whether it is faster. The bench's coordination does not see that run, "
            "and the board it drives is the one this incident is about.",
        ),
    ),
    "timeout:stlink": ErrorRemedy(
        meaning=(
            "STM32CubeProgrammer (STM32_Programmer_CLI), ST-LINK_gdbserver, or the GDB this backend reads symbols out "
            "of the flashed ELF with or drives a debug session through, did not finish before its deadline: the "
            "version check, the probe listing, a run for probe_target, flash_firmware, reset_target or a memory read, "
            "a symbol lookup, the debug server's ready line at a session start (`backend_error_type` "
            "`gdb_server_not_ready`), or one GDB/MI command inside a session. A process that runs out of time is "
            "stopped at the deadline, so the result knows about the board only what its state fields say. The probe "
            "listing and the symbol lookup never contact the target."
        ),
        remediation=(
            "Read `side_effect_status`, `target_contacted` and `target_state` first, where the result carries them. A "
            "flash, a reset or a session command that timed out can have stopped partway, and the log at `log_path` "
            "holds what STM32CubeProgrammer or ST-LINK_gdbserver printed before it was stopped.",
            "When `target_state` is `unknown` inside a debug session, call debug_halt: a confirmed halt settles the "
            "unconfirmed state, and the session goes on from the halted core.",
            "`gdb_server_not_ready` means ST-LINK_gdbserver did not print `Waiting for debugger connection...` in "
            "time; the server output in the log at `log_path` says why.",
            "The deadline comes from `debuggers.<name>.timeout_s`, and a slow bench that needs more is the operator's "
            "edit of the authoritative file: project_config_set does not write that key.",
        ),
        do_not=(
            "Do not repeat a flash, a reset or a memory read before the state fields say where the one that timed out "
            "left the board. A second run over an unknown state adds a second unknown.",
            "Do not run `STM32_Programmer_CLI` by hand to see whether it is faster. The bench's coordination does not "
            "see that run, and the board it drives is the one this incident is about.",
        ),
    ),
    "debugger_not_found:openocd": ErrorRemedy(
        meaning=(
            "The OpenOCD this entry needs could not be run: `debuggers.<name>.executable` names nothing that exists, "
            "or, left unset, no `openocd` is on PATH. A debug session start reports the same when the operating system "
            "refused to spawn its debug server, with the reason in `backend_error`. Nothing was started, so the target "
            "was not contacted and the board is as the last call that reached it left it."
        ),
        remediation=(
            "Read `backend_error` where the result carries it, and `likely_causes`: they say whether the binary is "
            "missing or the spawn was refused.",
            "Install OpenOCD (0.11.0 or newer for debug sessions) and put it on PATH, or name its binary by absolute "
            "path in `debuggers.<name>.executable`. That key is written with project_config_set behind "
            "`allow_config_description_write`, and which toolchain a bench runs is the operator's, so get their word.",
            "Run `agentic-hil doctor` afterwards: it repeats the lookup and reports the version once OpenOCD runs.",
        ),
        do_not=(
            "Do not copy an OpenOCD binary into the workspace and point the configuration at it. A configured "
            "executable inside the workspace is repository-controlled code running as the debugger.",
            "Do not run `openocd` by hand to get past it. A server this service did not start is one its coordination "
            "cannot see, stop or account for.",
        ),
    ),
    "debugger_not_found:pyocd": ErrorRemedy(
        meaning=(
            "The pyOCD this entry needs could not be run: `debuggers.<name>.executable` names nothing that exists, or, "
            "left unset, no `pyocd` is on PATH. A debug session start reports the same when the operating system "
            "refused to spawn its debug server, `pyocd gdbserver`, with the reason in `backend_error`. Nothing was "
            "started, so the target was not contacted and the board is as the last call that reached it left it."
        ),
        remediation=(
            "Read `backend_error` where the result carries it: it says why the spawn was refused.",
            "Install pyOCD into the environment the server runs from (`pip install agentic-hil[pyocd]` or "
            "`pip install pyocd`) so `pyocd` is on PATH, or name its binary by absolute path in "
            "`debuggers.<name>.executable`. That key is written with project_config_set behind "
            "`allow_config_description_write`, and which toolchain a bench runs is the operator's, so get their word.",
            "Run `agentic-hil doctor` afterwards: it repeats the lookup and reports the version once pyOCD runs.",
        ),
        do_not=(
            "Do not install pyOCD into the workspace and point the configuration at it. A configured executable inside "
            "the workspace is repository-controlled code running as the debugger.",
            "Do not run `pyocd` by hand to get past it. A run this service did not start is one its coordination "
            "cannot see or account for.",
        ),
    ),
    "debugger_not_found:stlink": ErrorRemedy(
        meaning=(
            "The STM32CubeProgrammer command-line tool this entry needs, STM32_Programmer_CLI, could not be run: "
            "`debuggers.<name>.executable` names nothing that exists, or, left unset, it is neither on PATH nor in the "
            "standard STM32CubeProgrammer and STM32CubeIDE install locations. At a debug session start it can instead "
            "be ST-LINK_gdbserver (`backend_error_type` `gdb_server_not_found`): the path "
            "`debuggers.<name>.gdb_server_executable` resolved to when the configuration loaded holds no file any "
            "more, and `field` names that key. A session start reports the same when the operating system refused to "
            "spawn the server, with the reason in `backend_error`. The target was not contacted, and the board is as "
            "the last call that reached it left it."
        ),
        remediation=(
            "Install STM32CubeProgrammer, which brings STM32_Programmer_CLI, or put the directory that holds it on "
            "PATH.",
            "For `gdb_server_not_found`, reinstall STM32CubeCLT, which brings ST-LINK_gdbserver, or name the server by "
            "absolute path in `debuggers.<name>.gdb_server_executable`. The path is resolved when the configuration "
            "loads, so a server installed elsewhere is adopted through `project_config_reload_description` or a "
            "restart.",
            "Where it lives somewhere else, name the binary by absolute path in `debuggers.<name>.executable`. That key "
            "is written with project_config_set behind `allow_config_description_write`, and which toolchain a bench "
            "runs is the operator's, so get their word.",
            "Run `agentic-hil doctor` afterwards: it repeats the lookup and reports the version once the tool runs.",
        ),
        do_not=(
            "Do not copy STM32_Programmer_CLI into the workspace and point the configuration at it. A configured "
            "executable inside the workspace is repository-controlled code running as the debugger.",
            "Do not run STM32_Programmer_CLI or ST-LINK_gdbserver by hand to get past it. A run this service did not "
            "start is one its coordination cannot see or account for.",
        ),
    ),
    "debugger_not_found": ErrorRemedy(
        meaning=(
            "The debugger program a call needed could not be run: it is not where the configuration points, not on "
            "PATH, or it was found and disappeared before it could be started. Probe discovery says so when neither "
            "STM32CubeProgrammer's command-line tool nor OpenOCD is installed. Nothing was started, so the target was "
            "not contacted."
        ),
        remediation=(
            "Read `summary`, and `executable` or `tools_searched` where present: they name the program that was looked "
            "for and where.",
            "Install the toolchain the summary names and put it on PATH, or, for a configured entry, name the binary by "
            "absolute path in `debuggers.<name>.executable` with the operator's word.",
            "Run `agentic-hil doctor` afterwards: it repeats the lookup and reports what it finds.",
        ),
        do_not=(
            "Do not copy a toolchain binary into the workspace and point the configuration at it. A configured "
            "executable inside the workspace is repository-controlled code running as the debugger.",
            "Do not drive the probe by hand with whatever debugger happens to be installed. The bench's coordination "
            "does not see that run.",
        ),
    ),
    "config_file_not_found:openocd": ErrorRemedy(
        meaning=(
            "OpenOCD, started as a debug session's server, exited because it could not find a file it was told to "
            "read, and that file is neither of the two scripts the entry names: most often a script `interface_cfg` or "
            "`target_cfg` pulls in with `source [find ...]`, missing from this OpenOCD's script tree. This is "
            "OpenOCD's script, not the Agentic HIL configuration file. The server stopped before the target was "
            "reached."
        ),
        remediation=(
            "Read the server output in the log at `log_path` (`server_stderr_tail`): OpenOCD names the file it could "
            "not find.",
            "Check that the OpenOCD this entry runs has a complete script tree. The file has to resolve wherever "
            "`interface_cfg` and `target_cfg` resolve, and `agentic-hil doctor` says of each whether it is a search "
            "name or a path, and for a path whether the file is there.",
            "Install the missing scripts or a complete OpenOCD, or point `OPENOCD_SCRIPTS` at the tree that has them, "
            "then start the session again with debug_start_session.",
        ),
        do_not=(
            "Do not copy OpenOCD scripts into the repository to supply the missing file. A script inside the "
            "workspace is repository-controlled Tcl running in the debugger.",
            "Do not run `openocd` directly to get past it.",
        ),
    ),
    "not_supported:openocd": ErrorRemedy(
        meaning=(
            "debugger_probes_list has no enumeration for this entry's adapter. OpenOCD has no command that lists "
            "connected probes, and this host lists a probe from its USB serial inventory only for adapters whose USB "
            "identity it can read there; `interface_cfg` names another one. Nothing was contacted."
        ),
        remediation=(
            "Read the probe's serial off its label, or off the adapter vendor's own listing tool, and record it as "
            "`debuggers.<name>.probe_id` with project_config_set behind `allow_config_description_write`, with the "
            "operator's word.",
            "The calls that act on the probe, probe_target, flash_firmware, reset_target and the debug session, select "
            "it by that `probe_id` and are unaffected by this refusal.",
        ),
        do_not=(
            "Do not guess a `probe_id` from a vendor id or a port name. A selector that matches the wrong probe is the "
            "wrong-board risk the id exists to rule out.",
        ),
    ),
    "audit_broken:openocd": ErrorRemedy(
        meaning=(
            "The debug session's own evidence could not be written: the audit record of a GDB command, or the session "
            "log at `log_path`, failed to persist (`backend_error_type` `audit_write_failed`). The service latches on "
            "the first such failure. From then on it refuses every new debug session and every GDB command that is "
            "not containment, debug_halt still runs, and the quarantine stands. A start refusal with this type is that "
            "latch, set by an earlier failure."
        ),
        remediation=(
            "Hand it to the operator. The audit destination is fixed first: free disk space, and permissions on the "
            "reports and logs directories under `state_root`.",
            "The operator then restarts the MCP server, which is what clears the latch, checks the board against the "
            "last committed report (`get_last_report`), and ends the incident with `agentic-hil recover`.",
        ),
        do_not=(
            "Do not retry the session or its commands to get the evidence written. Every call after the latch is "
            "refused, and the one that failed left no record of itself.",
            "Do not start `openocd` and a GDB by hand to go on debugging. The latch refuses sessions so that no GDB "
            "command reaches the board unrecorded, and a server this service did not start is one its coordination "
            "cannot see, stop or account for.",
            "Do not delete or edit reports or logs to make room. They are the evidence the operator checks the board "
            "against.",
            "Do not expect hardware_recover to settle it. A broken audit is the operator's own route.",
        ),
    ),
    "audit_broken:pyocd": ErrorRemedy(
        meaning=(
            "The debug session's own evidence could not be written: the audit record of a GDB command, or the session "
            "log at `log_path`, failed to persist (`backend_error_type` `audit_write_failed`). The service latches on "
            "the first such failure. From then on it refuses every new debug session and every GDB command that is "
            "not containment, debug_halt still runs, and the quarantine stands. A start refusal with this type is that "
            "latch, set by an earlier failure."
        ),
        remediation=(
            "Hand it to the operator. The audit destination is fixed first: free disk space, and permissions on the "
            "reports and logs directories under `state_root`.",
            "The operator then restarts the MCP server, which is what clears the latch, checks the board against the "
            "last committed report (`get_last_report`), and ends the incident with `agentic-hil recover`.",
        ),
        do_not=(
            "Do not retry the session or its commands to get the evidence written. Every call after the latch is "
            "refused, and the one that failed left no record of itself.",
            "Do not start `pyocd gdbserver` and a GDB by hand to go on debugging. The latch refuses sessions so that no GDB "
            "command reaches the board unrecorded, and a server this service did not start is one its coordination "
            "cannot see, stop or account for.",
            "Do not delete or edit reports or logs to make room. They are the evidence the operator checks the board "
            "against.",
            "Do not expect hardware_recover to settle it. A broken audit is the operator's own route.",
        ),
    ),
    "audit_broken:stlink": ErrorRemedy(
        meaning=(
            "The debug session's own evidence could not be written: the audit record of a GDB command, or the session "
            "log at `log_path`, failed to persist (`backend_error_type` `audit_write_failed`). The service latches on "
            "the first such failure. From then on it refuses every new debug session and every GDB command that is "
            "not containment, debug_halt still runs, and the quarantine stands. A start refusal with this type is that "
            "latch, set by an earlier failure."
        ),
        remediation=(
            "Hand it to the operator. The audit destination is fixed first: free disk space, and permissions on the "
            "reports and logs directories under `state_root`.",
            "The operator then restarts the MCP server, which is what clears the latch, checks the board against the "
            "last committed report (`get_last_report`), and ends the incident with `agentic-hil recover`.",
        ),
        do_not=(
            "Do not retry the session or its commands to get the evidence written. Every call after the latch is "
            "refused, and the one that failed left no record of itself.",
            "Do not start ST-LINK_gdbserver and a GDB by hand to go on debugging. The latch refuses sessions so that no GDB "
            "command reaches the board unrecorded, and a server this service did not start is one its coordination "
            "cannot see, stop or account for.",
            "Do not delete or edit reports or logs to make room. They are the evidence the operator checks the board "
            "against.",
            "Do not expect hardware_recover to settle it. A broken audit is the operator's own route.",
        ),
    ),
    "adapter_access_denied": ErrorRemedy(
        meaning=(
            "OpenOCD reached the probe on USB and was refused opening it: libusb answered `LIBUSB_ERROR_ACCESS`. The "
            "probe is attached, and this user may not open its USB device. The debug session start reports it under "
            "this name; probe_target, flash_firmware and reset_target report it as `adapter_not_found` with this "
            "`backend_error_type`. It is read only off Windows: there the same libusb error can also mean that another "
            "program holds the device. "
            "The target was not reached."
        ),
        remediation=(
            "Have the operator give this user access to the probe's USB device: install the udev rule for the probe "
            "(OpenOCD ships one as `60-openocd.rules`) and add this user to the group the rule gives the device to, "
            "plugdev on Debian and Ubuntu, then log in again so the new group applies.",
            "`ls -l /dev/bus/usb/<bus>/<device>`, with the bus and device numbers lsusb prints for the probe, shows the "
            "owner, group and mode the device node has.",
            "Start the session again with debug_start_session once the user is in that group.",
        ),
        do_not=(
            "Do not run the server or OpenOCD as root, or through sudo, to get past it. The debugger would then run "
            "with every right on the host, and the next start as this user fails the same way.",
        ),
    ),
    "breakpoint_reconciliation_failed": ErrorRemedy(
        meaning=(
            "debug_clear_breakpoints could not prove that the backend holds no breakpoints: GDB's breakpoint list "
            "could not be read, or it still listed breakpoints after the deletes (`remaining_backend_breakpoints`). The "
            "cleanup is unconfirmed: `cleanup_required` is true and `side_effect_status` is `unknown`."
        ),
        remediation=(
            "Read `remaining_backend_breakpoints` and the log at `log_path`: the numbers GDB still lists, or why its "
            "list could not be read.",
            "Call debug_clear_breakpoints again. A retry reads GDB's own list first and deletes only what GDB reports, "
            "so it is safe to repeat.",
            "A result with `backend_reconciled` true settles the unconfirmed cleanup, and the session goes on from "
            "there.",
        ),
        do_not=(
            "Do not call debug_continue while the clear is unconfirmed. A breakpoint GDB still holds can stop the "
            "target where the test does not expect it.",
        ),
    ),
    "debug_session_setup_failed": ErrorRemedy(
        meaning=(
            "debug_start_session spawned the debug server and could not start the threads that read its output, so "
            "the start was abandoned before GDB ran. `backend_error` holds the host's reason, usually a host out of "
            "threads or memory. The server was then stopped: `cleanup_confirmed` says it was, and `cleanup_required` "
            "with `cleanup_error` says it could not be."
        ),
        remediation=(
            "Read `backend_error`, and `cleanup_required` and `cleanup_error` for what is left: a cleanup that failed "
            "leaves a debug server the session still owns.",
            "When `cleanup_confirmed` and `retry_safe` are both true, the server is gone and nothing reached the "
            "target: call debug_start_session again once the host has the resources back.",
            "When `cleanup_required` is true, the probe is held under an incident, and the quarantine guidance on the "
            "result names what settles it.",
        ),
        do_not=(
            "Do not kill the leftover debug server by hand. The session still owns it, and the recovery that reaps it "
            "records that it did.",
        ),
    ),
    "gdb_start_failed": ErrorRemedy(
        meaning=(
            "debug_start_session started the debug server, and the GDB `debug.gdb_executable` names could not be "
            "started for GDB/MI. `backend_error` holds the reason. The debug server was then stopped: "
            "`cleanup_confirmed` says it was, and `cleanup_required` with `cleanup_error` says it could not be."
        ),
        remediation=(
            "Read `backend_error`, and the configured `debug.gdb_executable`, which project_config_describe reports. A "
            "GDB built for another machine, or against a Python or a shared library this host lacks, exits at once.",
            "Point `debug.gdb_executable` at a GDB that runs on this host and knows this target's architecture "
            "(`arm-none-eabi-gdb` or `gdb-multiarch` for an Arm Cortex-M part) with project_config_set, with the "
            "operator's word, and "
            "restart the MCP server, which reads `debug` only at startup.",
            "Read `cleanup_required` and `cleanup_error` as well: a server the cleanup could not stop still holds the "
            "probe under an incident, and the quarantine guidance on the result names what settles it.",
            "When `cleanup_confirmed` and `retry_safe` are both true, call debug_start_session again once GDB runs.",
        ),
        do_not=(
            "Do not drive the board with a GDB started by hand to get past it. A session outside the service has no "
            "evidence, no breakpoint ledger and no containment.",
        ),
    ),
    "gdb_async_unsupported": ErrorRemedy(
        meaning=(
            "The GDB this bench names refused `-gdb-set mi-async on`. A debug session needs asynchronous GDB/MI to "
            "interrupt a running target when a wait times out, so this GDB cannot run one. The refusal comes before "
            "the target is connected: `target_contacted` is false and the board did not change."
        ),
        remediation=(
            "Read `backend_error` for GDB's own words.",
            "Point `debug.gdb_executable` at a GDB release that accepts asynchronous MI; GDB 7.8 and newer have the "
            "setting. The change is project_config_set behind `allow_config_description_write`, with the operator's "
            "word, and the MCP server restarts to use it, because it reads `debug` only at startup.",
        ),
        do_not=(
            "Do not repeat debug_start_session with the same GDB. It refuses the setting the same way every time.",
            "Do not run a session in synchronous MI by hand. A timeout in it cannot interrupt the target, and the "
            "board keeps running with nobody watching it.",
        ),
    ),
    "interface_config_not_found": ErrorRemedy(
        meaning=(
            "OpenOCD, started as a debug session's server, exited because it could not find the script "
            "`debuggers.<name>.interface_cfg` names. The debug session start reports it under this name; probe_target, "
            "flash_firmware and reset_target report the same failure as `debugger_config_not_found`. The server stopped "
            "before the target was reached."
        ),
        remediation=(
            "Read the server output in the log at `log_path`: OpenOCD names what it looked for.",
            "Check `debuggers.<name>.interface_cfg` with project_config_describe or `agentic-hil doctor`. A search "
            "name such as `interface/stlink.cfg` has to resolve in this OpenOCD's script tree, and a path has to be "
            "absolute, exist and lie outside the workspace.",
            "Correct it with project_config_set behind `allow_config_description_write`, with the operator's word, or "
            "install the OpenOCD scripts the search name expects, then start the session again with "
            "debug_start_session.",
        ),
        do_not=(
            "Do not copy OpenOCD scripts into the repository and point the configuration at them. A script inside the "
            "workspace is repository-controlled Tcl running in the debugger.",
            "Do not run `openocd` directly to get past it.",
        ),
    ),
    "target_config_not_found": ErrorRemedy(
        meaning=(
            "OpenOCD, started as a debug session's server, exited because it could not find the script "
            "`debuggers.<name>.target_cfg` names. The debug session start reports it under this name; probe_target, "
            "flash_firmware and reset_target report the same failure as `debugger_config_not_found`. The server stopped "
            "before the target was reached."
        ),
        remediation=(
            "Read the server output in the log at `log_path`: OpenOCD names what it looked for.",
            "Check `debuggers.<name>.target_cfg` with project_config_describe or `agentic-hil doctor`. It has to match "
            "the MCU family; a search name such as `target/stm32f4x.cfg` has to resolve in this OpenOCD's script tree, "
            "and a path has to be absolute, exist and lie outside the workspace.",
            "Correct it with project_config_set behind `allow_config_description_write`, with the operator's word, or "
            "install the OpenOCD scripts the search name expects, then start the session again with "
            "debug_start_session.",
        ),
        do_not=(
            "Do not copy OpenOCD scripts into the repository and point the configuration at them. A script inside the "
            "workspace is repository-controlled Tcl running in the debugger.",
            "Do not run `openocd` directly to get past it.",
        ),
    ),
    "session_already_active": ErrorRemedy(
        meaning=(
            "debug_start_session was refused because this server still has a debug session that has not ended: a "
            "running one, or one left in `cleanup_required` by a stop or a start that could not finish. `session` in "
            "the refusal describes it. Nothing was started."
        ),
        remediation=(
            "Read `session` in the refusal: its status says whether it is running or waiting on a cleanup.",
            "End a running one with debug_stop_session, then call debug_start_session for the new one.",
            "One in `cleanup_required` is held under the incident its last result named. Read that result's own entry "
            "first, because a stop repeated over an unconfirmed halt settles nothing.",
        ),
        do_not=(
            "Do not kill the debug server or GDB by hand to free the probe. The session record still names them, and "
            "the next start is refused the same way.",
        ),
    ),
    "stop_reason_not_available": ErrorRemedy(
        meaning=(
            "debug_get_stop_reason had no stop to report: this session has recorded no stop yet. The session is "
            "active, and nothing changed on the target."
        ),
        remediation=(
            "Run the target with debug_continue to a breakpoint, or stop it where it is with debug_halt; then call "
            "debug_get_stop_reason.",
        ),
        do_not=(
            "Do not read this refusal as a target that is running, or as one that stopped cleanly. It says only that "
            "no stop has been recorded.",
        ),
    ),
    "target_exception": ErrorRemedy(
        meaning=(
            "The target stopped in an exception or a fault: a debug session found the core halted in its handler "
            "(`stop_reason` `exception` or `fault`) on a continue, a halt or right at the attach. A session reports "
            "it with `ok` true, `target_ok` false and `target_error_type` `target_exception`, and a test reactor step "
            "publishes the same type as its own `error_type`. The core is halted, and the stop record says why."
        ),
        remediation=(
            "Read the stop record first: `frame` (function, address, file, line), `exception_type`, `fault_type` and "
            "`signal` say where the core stopped and what it took.",
            "Collect the evidence the diagnosis needs while the core is still halted: debug_symbol_value for the "
            "variables that matter, debug_dump_symbol_ihex for a buffer or a fault log in memory, and the firmware's "
            "own log.",
            "Once the evidence is in hand, end the session with debug_stop_session and bring the target back from reset "
            "with reset_target, or start a fresh session with debug_start_session, before the test runs again.",
        ),
        do_not=(
            "Do not call debug_continue to get past it. The core goes back into the handler or takes the same fault "
            "again, and the stop record that says where it happened is replaced.",
        ),
    ),
    "unexpected_breakpoint": ErrorRemedy(
        meaning=(
            "The target stopped at a breakpoint the session did not expect: one GDB holds that this session did not "
            "set or set and forgot, or a `BKPT` instruction or an assert in the firmware itself. A session reports it "
            "with `ok` true, `target_ok` false and `target_error_type` `unexpected_breakpoint`, and a test reactor "
            "step publishes the same type as its own `error_type`. The core is halted there."
        ),
        remediation=(
            "Read `frame` and `backend_breakpoint_id` in the stop record, and compare them with what "
            "debug_list_breakpoints reports.",
            "For a stale breakpoint, call debug_clear_breakpoints and set only the expected ones again with "
            "debug_set_breakpoint.",
            "For a `BKPT` or an assert in the firmware, collect the log and the memory evidence, then reset the target "
            "or restart the debug session.",
        ),
        do_not=(
            "Do not call debug_continue blindly. The target is halted where nobody planned it to stop, and resuming "
            "runs on from a state the test did not set up.",
        ),
    ),
    "debugger_error": ErrorRemedy(
        meaning=(
            "The debugger failed, and its output matched none of the failures this server classifies. The backend's "
            "own name for it travels in `backend_error_type`: `unknown_debugger_error` for a probe, flash or reset run, "
            "`gdb_error` for a GDB/MI command in a debug session. A debug session can also stop with `stop_reason` "
            "`debugger_error`, and a test reactor step publishes the same type as its own `error_type`."
        ),
        remediation=(
            "Read what the debugger printed: the log at `log_path`, and `programmer_output` where the result carries "
            "it, end with the debugger's own words for what went wrong.",
            "Call classify_last_error: it reads the last failure report back and names its classification and likely "
            "causes.",
            "Read `side_effect_status` and `target_state` before the next call that drives the board. A failure after "
            "the target was contacted leaves it where the debugger stopped.",
        ),
        do_not=(
            "Do not repeat the call unchanged before the output is read. An unclassified failure names no cause, and a "
            "repeat over a contacted target can add a second unknown effect to the first.",
        ),
    ),
    "unknown_debugger_error": ErrorRemedy(
        meaning=(
            "classify_last_error read back the last failure record and found no error type in it: the record named "
            "neither `error_type` nor `target_error_type` and did not fail its audit, and it still failed one of the "
            "checks every result is held to: `ok` not true, `target_ok` or `cleanup_ok` false, `cleanup_required` or "
            "`quarantined` true, a `lease_state` that is set and is neither `active` nor `released` (a missing or "
            "null one passes this check), `side_effect_status` `unknown` or `partial`, or `hardware_state` "
            "`unknown`. This name stands in for the missing type. It is not something a debugger reported, unlike "
            "the `backend_error_type` of the same name a `debugger_error` carries. `source_tool` names the call that "
            "wrote the record, and `summary` is that call's own sentence."
        ),
        remediation=(
            "Read `summary`, `source_tool` and `log_path` on this result: with no error type, they are what says "
            "what happened. While no other call has finished since, `get_last_report` returns the whole record, and "
            "the fields named above say which check it failed.",
            "If the record has `quarantined` or `cleanup_required` true, or a `lease_state` that is set and is neither "
            "`active` nor `released` (a missing or null one is no reason), call `hardware_lease_status` and settle "
            "what it holds before the next hardware call.",
            "If `side_effect_status` is `unknown` or `partial`, or `hardware_state` is `unknown`, treat the board's "
            "state as unknown until a later call confirms it, such as a `probe_target` that succeeds.",
        ),
        do_not=(
            "Do not report this as a debugger fault. The name stands for a missing error type, and the failing call's "
            "own words are in `summary` and `log_path`.",
            "Do not read this classification's own `ok: true` as the call having passed. It says the record was read "
            "back; the record itself is a failure.",
        ),
    ),
    "reset_failed": ErrorRemedy(
        meaning=(
            "A reset the debugger was asked for did not complete: the backend reported a reset failure, or the reset "
            "never printed its success marker. It is reset_target's own failure, or the reset after a flash: a "
            "flash_firmware result with `side_effect_status` `partial` wrote the image, and only the reset after it "
            "failed. A test reactor step publishes the same type as its own `error_type`."
        ),
        remediation=(
            "Read `side_effect_status`, `target_state` and `quarantined` first: they say whether the reset was "
            "attempted and whether the board is now held under an incident.",
            "Call probe_target to read the target back. Where the bench's `recovery.auto_recover` is `reset_halt` and "
            "the probe grants `allow_reset`, the automatic recovery runs first, drives the target into a defined "
            "halted state and reads it back, which ends the incident; elsewhere the incident is the operator's.",
            "Check what `likely_causes` names before the next reset: the reset line between the in-circuit debugger or "
            "programmer and the target, the target's power, and for OpenOCD the `reset_config` in `target_cfg`.",
        ),
        do_not=(
            "Do not repeat reset_target or flash_firmware to get past it before the state fields and probe_target "
            "have said where the target is. A second reset over an unconfirmed one adds a second unknown, and a "
            "reflash over a partial one writes the board blind.",
        ),
    ),
    "probe_discovery_failed": ErrorRemedy(
        meaning=(
            "Probe discovery could not run, so it says nothing about which probes are attached: the debugger's listing "
            "command failed or answered something that is not a probe listing, or the USB serial inventory a listing "
            "reads could not be read. Nothing was contacted."
        ),
        remediation=(
            "Read `summary` first, and `backend_error` or `programmer_output` where present: `discovered_by` says "
            "which listing ran, and these say what it answered.",
            "Fix what they name, the debugger install, the serial backend or a probe that USB does not see, and call "
            "debugger_probes_list again.",
        ),
        do_not=(
            "Do not set, change or remove `probe_id` on the strength of this result. The listing did not run, so an "
            "empty or partial answer is no evidence about the bench.",
        ),
    ),
    "output_write_failed": ErrorRemedy(
        meaning=(
            "debug_dump_symbol_ihex could not leave the Intel HEX file at `output_path`: its directory could not be "
            "prepared, the file could not be written, or the programmer confirmed the read and left no parseable Intel "
            "HEX behind. The failure is the file's. A test reactor step publishes the same type as its own "
            "`error_type`."
        ),
        remediation=(
            "Read `backend_error` where present: the file system's own reason, such as a missing or read-only "
            "directory, a path outside the workspace, or a full disk.",
            "Call debug_dump_symbol_ihex again with an `output_path` inside the workspace that this user can write.",
            "Read `target_contacted`: true means the bytes left the target and only the file is missing, and a debug "
            "session's dump read the target before writing too.",
        ),
        do_not=(
            "Do not reset or reflash the target over this. The read leaves the target as it found it, and the failure "
            "is the output file's.",
        ),
    ),
    "symbol_not_found": ErrorRemedy(
        meaning=(
            "The symbol passed `debug.allowed_symbols` and is absent from the symbol table of the ELF that describes "
            "the target: misspelled, removed by the compiler or the linker, or never part of this build. A test "
            "reactor step publishes the same type as its own `error_type`."
        ),
        remediation=(
            "Check the name against the firmware source and the linker map: it is matched as the linked identifier.",
            "If it is defined and missing from the ELF, the build dropped it: an unused object is removed by the "
            "linker's garbage collection and a static one can be folded away. Mark it `volatile` or "
            "`__attribute__((used))`, rebuild, and flash the new ELF with flash_firmware.",
        ),
        do_not=(
            "Do not add names to `debug.allowed_symbols` to get past this. The allowlist was passed, and widening it "
            "changes what may be read, not what is in the image.",
        ),
    ),
    "symbol_resolution_failed": ErrorRemedy(
        meaning=(
            "GDB found no usable address or size for the symbol: it answered something this server could not parse, "
            "or the ELF's symbol table has the name without a size, more than once, or could not be read at all. A "
            "test reactor step publishes the same type as its own `error_type`."
        ),
        remediation=(
            "Read `symbol_table_lookup` where present, and `summary`. `no_size` is a symbol the ELF lists without a "
            "size, which an assembly label without a `.size` directive is; `ambiguous` is a name defined more than "
            "once; `unreadable` is an ELF the server could not parse.",
            "Give the object a size and a single definition the toolchain records (a C object, or `.size` on an "
            "assembly label), rebuild, flash it with flash_firmware, and confirm it with debug_symbol_info before "
            "reading it.",
        ),
        do_not=(
            "Do not repeat the same read unchanged. The ELF and the GDB that answered are the same, so the answer is "
            "too.",
        ),
    ),
    "symbol_ambiguous": ErrorRemedy(
        meaning=(
            "The name matches more than one symbol in the ELF, typically a `static` object defined in several "
            "translation units, and GDB will not pick one. A test reactor step publishes the same type as its own "
            "`error_type`."
        ),
        remediation=(
            "Give the object a name that is unique in the image, then rebuild and flash it with flash_firmware.",
        ),
        do_not=(
            "Do not read an address by hand in place of the symbol. The value tools read only a name that resolves to "
            "one object, so the guess would bypass the check that makes the read mean something.",
        ),
    ),
    "symbol_source_changed": ErrorRemedy(
        meaning=(
            "The ELF flashed through this service has changed on disk since it was flashed (its digest no longer "
            "matches, because it was rebuilt or replaced), or it can no longer be read, so its symbol table is not "
            "proven to describe the image on the target. Nothing was contacted. A test reactor step publishes the "
            "same type as its own `error_type`."
        ),
        remediation=(
            "Flash the current build with flash_firmware, so the image on the target and the ELF on disk are the same "
            "file again.",
            "Then call debug_symbol_info, debug_symbol_value or debug_dump_symbol_ihex again.",
        ),
        do_not=(
            "Do not read symbols out of the rebuilt ELF by hand with GDB. Its addresses describe a build that is not "
            "on the target.",
        ),
    ),
    "symbol_source_not_available": ErrorRemedy(
        meaning=(
            "No ELF has been flashed through this service, so no symbol table is known to describe the image on the "
            "target. The pyOCD and STM32CubeProgrammer backends answer symbol reads out of the ELF the last "
            "successful flash_firmware wrote, and there is none: no flash has succeeded in this session, the image was "
            "a .hex or a .bin, or the firmware was flashed outside Agentic HIL. Nothing was contacted. A test reactor "
            "step publishes the same type as its own `error_type`."
        ),
        remediation=(
            "Flash the ELF itself with flash_firmware: the `.elf` the build produced carries the symbols, and the "
            "flash records it.",
            "Then call debug_symbol_info, debug_symbol_value or debug_dump_symbol_ihex again.",
        ),
        do_not=(
            "Do not flash a .hex or a .bin to get symbols. Neither carries a symbol table, and the reads stay refused.",
        ),
    ),
    "cleanup_failed": ErrorRemedy(
        meaning=(
            "A cleanup could not finish. In debug_stop_session it is the session's processes: GDB or the debug server "
            "could not be stopped (`cleanup_error`), the session stays `cleanup_required` with `hardware_state` "
            "unknown, and `halt_not_confirmed`, `breakpoints_removed_confirmed` and `detach_resume_guard_confirmed` "
            "say whether the target was proven halted, rid of the session's breakpoints and kept from resuming before "
            "that. In a test reactor run it is the run's teardown: "
            "`cleanup_errors` lists each device and action that failed, and `step_error_type` keeps the failure that "
            "came before it."
        ),
        remediation=(
            "Read `cleanup_error`, or each entry of `cleanup_errors`, with the log at `log_path`: they name what could "
            "not be stopped or closed.",
            "Where it is the debug session, call probe_target. The automatic recovery the bench's "
            "`recovery.auto_recover` allows runs first: it reaps leftover debugger processes and reads the target "
            "back, and a confirmed read ends the incident and the session with it.",
            "When `halt_not_confirmed` is false and `breakpoints_removed_confirmed` and `detach_resume_guard_confirmed` "
            "are true, debug_stop_session called once more repeats only the process cleanup and can finish it.",
            "An entry for a COM port or a CAN bus is that session's own teardown, which a probe read cannot speak for: "
            "the quarantine guidance on the result names what settles it. Where `recovery.auto_recover` is `off`, the "
            "incident is the operator's to end with `agentic-hil recover`.",
        ),
        do_not=(
            "Do not call debug_stop_session again while `halt_not_confirmed` is true or `breakpoints_removed_confirmed` "
            "or `detach_resume_guard_confirmed` is false. Over an unconfirmed target state a repeated stop forces every "
            "proof false and settles nothing.",
            "Do not start a new debug session over it. debug_start_session is refused as `session_already_active` "
            "until this one ends.",
        ),
    ),
    "halt_not_confirmed": ErrorRemedy(
        meaning=(
            "debug_stop_session cleaned up the session's processes and could not confirm that the target was halted "
            "before the session ended (`halt_not_confirmed` true). The session stays `cleanup_required` with "
            "`hardware_state` unknown: the core may be running whatever it ran when the connection went away."
        ),
        remediation=(
            "Call probe_target. The automatic recovery the bench's `recovery.auto_recover` allows runs first: it reaps "
            "any leftover debugger process and reads the target back through the probe, and a confirmed read ends the "
            "incident and the session with it; debug_get_session_status then reports it stopped.",
            "Where `recovery.auto_recover` is `off`, or the probe's `allow_probe` is closed, the incident is the "
            "operator's to end with `agentic-hil recover` after checking the board.",
        ),
        do_not=(
            "Do not call debug_stop_session again for this. A stop after an unconfirmed halt brings no new evidence: "
            "every proof is forced false and the incident stays where it is.",
            "Do not start a new debug session to get a fresh halt. debug_start_session is refused as "
            "`session_already_active` until this one ends.",
        ),
    ),
    "breakpoints_not_removed": ErrorRemedy(
        meaning=(
            "debug_stop_session confirmed the halt and cleaned up the session's processes, and could not confirm that "
            "this session's breakpoints were taken off the target before the session ended "
            "(`breakpoints_removed_confirmed` false). pyOCD and ST-LINK_gdbserver are ended before GDB detaches, so "
            "GDB's detach cannot carry the removal there: the stop deletes the breakpoints and reads the backend's "
            "list back first, and that delete or read failed, or the list still held some. The session stays "
            "`cleanup_required` with `hardware_state` unknown: a breakpoint left on the target is a hardware "
            "comparator the next opener of the probe can meet."
        ),
        remediation=(
            "Read the session log at `log_path`: `breakpoint_removal` names the stage the removal stopped at, and "
            "`remaining_backend_breakpoints` the numbers GDB still listed.",
            "Call probe_target. The automatic recovery the bench's `recovery.auto_recover` allows runs first: it reaps "
            "any leftover debugger process and reads the target back through the probe, and a confirmed read ends the "
            "incident and the session with it; debug_get_session_status then reports it stopped.",
            "Where `recovery.auto_recover` is `off`, or the probe's `allow_probe` is closed, the incident is the "
            "operator's to end with `agentic-hil recover` after checking the board.",
        ),
        do_not=(
            "Do not call debug_stop_session again for this. A stop after an unconfirmed removal brings no new evidence: "
            "every proof is forced false and the incident stays where it is.",
            "Do not start a new debug session to clear them. debug_start_session is refused as "
            "`session_already_active` until this one ends.",
        ),
    ),
    "detach_resume_not_confirmed": ErrorRemedy(
        meaning=(
            "debug_stop_session confirmed the halt and cleaned up the session's processes, and could not confirm the "
            "guard that keeps the backend from resuming the target when GDB detaches "
            "(`detach_resume_guard_confirmed` false). The session stays `cleanup_required` with `hardware_state` "
            "unknown: the target may have been resumed as the connection closed."
        ),
        remediation=(
            "Call probe_target. The automatic recovery the bench's `recovery.auto_recover` allows runs first: it reaps "
            "any leftover debugger process and reads the target back through the probe, and a confirmed read ends the "
            "incident and the session with it; debug_get_session_status then reports it stopped.",
            "Where `recovery.auto_recover` is `off`, or the probe's `allow_probe` is closed, the incident is the "
            "operator's to end with `agentic-hil recover` after checking the board.",
        ),
        do_not=(
            "Do not call debug_stop_session again for this. A stop after an unconfirmed detach brings no new evidence: "
            "every proof is forced false and the incident stays where it is.",
            "Do not start a new debug session to get a fresh halt. debug_start_session is refused as "
            "`session_already_active` until this one ends.",
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
    CAN_INTERFACE_DOWN_ERROR: ErrorRemedy(
        meaning=(
            "The SocketCAN interface named by `can_buses.<name>.channel` is on this host and is administratively "
            "down. The kernel lets a raw CAN socket bind a down interface and then answers every receive and every "
            "send on it with ENETDOWN, so a session opened over one would carry nothing in either direction: the "
            "state was read before the socket was opened, so no controller was addressed and nothing was put on any "
            "bus. This is a refusal about the host's network configuration, not a quarantine, and it is deliberately "
            "not the same answer as an interface that is absent: this one is here, under the configured name, and "
            "one command away from carrying frames. The usual cause is that it was created and never brought up, or "
            "that it was taken down out of band and nothing brought it back."
        ),
        remediation=(
            "Bring it up: `sudo ip link set <dev> up`, where `<dev>` is the `channel` on the result. A real CAN "
            "controller needs its bitrate set first, with `sudo ip link set <dev> type can bitrate <bitrate>`; a "
            "`vcan` needs nothing but the one command.",
            "Confirm it is this bench's interface before bringing it up: `ip -details link show dev <dev>` reports "
            "the state and, for a real controller, the bitrate it is configured for, and `can_buses_list` reports "
            "what this bench has configured under which name.",
            "Retry the session afterwards. The bench was never blocked: `retry_safe` is true, no incident was "
            "opened, and the same entry opens on the running server as soon as the link is up, with no restart.",
        ),
        do_not=(
            "Do not run `recover --confirm-safe-state` over this. There is nothing to recover: no lease was "
            "quarantined, and signing for a physical state nobody disturbed teaches the signature to mean nothing.",
            "Do not start the session with `clear_rx_queue: false` to get past it. That is what used to report a "
            "started session over a link that carries nothing, and the receive queue is not what is wrong here.",
            "Do not move the entry to another interface that happens to be up. That is the wrong-bus mistake the "
            "channel name exists to prevent, and the interface this one names is the one this bench is wired to.",
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
    "bridge_process_reap_failed": ErrorRemedy(
        meaning=(
            "Found under `cleanup_error` of a `can_session_start` refusal, never on its own. A CAN process bridge "
            "failed its open, this server ended the bridge's process tree, and it could not confirm that the process "
            "was gone: ending it raised, or the threads reading its output were still alive afterwards. A bridge that "
            "may still be running may still hold the adapter's channel, so the session stays registered with "
            "`cleanup_required: true` and keeps the bus."
        ),
        remediation=(
            "Read `backend_error` inside `cleanup_error`: it is the error ending the process raised, or the note that "
            "its output threads outlived it. A `close_response` beside it means the bridge did not confirm a safe "
            "state either.",
            "Look on this machine for the process the refusal's `command` started. While it runs it may still have "
            "the channel open, and the bus is not free until it has gone.",
            "The next `can_session_start` on this bus tears the registered session down first and retries the "
            "cleanup, and answers `can_adapter_close_failed` if that teardown fails too. When this refusal reports "
            "`quarantined: true`, that teardown fails on the lease whatever the bridge does, and the way out is the "
            "sign-off described under `resource_quarantined`.",
        ),
        do_not=(
            "Do not end processes by name to clear this. `command` names the bridge this session started, and a "
            "loose match may be another session's bridge or another program.",
            "Do not open the channel from another program while this session is registered: the bridge may still be "
            "driving it.",
        ),
    ),
    "bridge_safe_state_unconfirmed": ErrorRemedy(
        meaning=(
            "Found under `cleanup_error` of a `can_session_start` refusal, and in words as the `backend_error` of "
            "`can_adapter_close_failed`. A CAN process bridge was asked to put its controller in a safe state and "
            "close, never confirmed that it had, and this server then ended its process anyway. Only the bridge can "
            "confirm a safe state, so once its process has ended there is nothing left to confirm it with: the "
            "unconfirmed close is recorded under `cleanup_reasons`, and the bus is given back."
        ),
        remediation=(
            "Read `close_response`, the bridge's answer to the close: `can_adapter_timeout` means it did not answer "
            "in time, `can_adapter_process_exited` that it was already gone, `can_adapter_close_interrupted` that "
            "sending the close raised, and an answer without `safe_state_confirmed: true` that it replied without "
            "confirming.",
            "`safe_state_confirmed` stays false for that session. The `can_session_stop` or `can_session_start` "
            "that met the ended process answers `can_adapter_close_failed` once, ends the session and gives the bus "
            "back; the next `can_session_start` opens a fresh bridge.",
            "Check the bench by hand (the controller off the bus, the target in a known state) before that "
            "`can_session_start` puts the bus back to work.",
        ),
        do_not=(
            "Do not make a bridge answer `safe_state_confirmed: true` without having put its controller in a safe "
            "state. That field is the only evidence this server has about the bus.",
            "Do not take the next session's frames as a continuation of the old one. What the ended bridge last did "
            "on the bus is unknown.",
        ),
    ),
    "can_adapter_close_failed": ErrorRemedy(
        meaning=(
            "A CAN session could not be closed. `can_session_stop` was closing it, or `can_session_start` was "
            "replacing a session still registered on the bus or closing the one a failed receive-queue clear left. "
            "Either the adapter's close raised and the session stays registered on this server for a cleanup retry, "
            "or the adapter closed and the lease on the bus would not release, or a process bridge ended without "
            "confirming its close, which is final: then the summary says so, the session is ended and the bus given "
            "back, and the unconfirmed close is recorded under `cleanup_reasons`."
        ),
        remediation=(
            "Read `backend_error`. Present, it is what the adapter's close raised; absent, the adapter closed and the "
            "lease would not release, which `cleanup_reasons` and `quarantine_id` explain.",
            "On a direct adapter (`socketcan`, `peak`) whose close raised, `can_session_stop` called again runs the "
            "driver's shutdown again, and a shutdown that completes ends the session and frees the bus.",
            "A process bridge that ended without confirming a safe state cannot confirm it afterwards, so this is "
            "answered once: the bus is already given back. Check the bench by hand, then `can_session_start` opens "
            "a fresh bridge.",
            "Whenever the lease is quarantined, `quarantine_id` names the incident and `resource_quarantined` names "
            "the sign-off.",
            "`can_buses_list` shows the session still registered on the bus, with its `adapter_status`.",
        ),
        do_not=(
            "Do not call `can_session_stop` or `can_session_start` over and over while the driver's shutdown keeps "
            "raising. Each call retries the same close; fix what `backend_error` names first.",
            "Do not open the adapter or its channel from another program while the session is registered: an adapter "
            "whose close failed may still have the channel open.",
        ),
    ),
    "can_adapter_close_interrupted": ErrorRemedy(
        meaning=(
            "Found as the `close_response` under a `cleanup_error`, never on its own. Sending the close request to a "
            "CAN process bridge raised or was interrupted before the bridge answered, so whether the bridge put its "
            "controller in a safe state is unknown, and this server went on to end its process."
        ),
        remediation=(
            "Read the `cleanup_error` this sits in. Its type, `bridge_safe_state_unconfirmed` or "
            "`bridge_process_reap_failed`, says what became of the process, and that entry says what to do next.",
            "`stderr_tail`, when present, is the bridge's last output before the interrupt and may show what it was "
            "doing with the controller.",
        ),
        do_not=(
            "Do not take an interrupted close for a close that never started. The request may have reached the "
            "bridge, and the bridge may have acted on part of it.",
        ),
    ),
    "can_adapter_invalid_request": ErrorRemedy(
        meaning=(
            "A request to the CAN process bridge (`open`, `send`, `read` or `close`) could not be serialized or "
            "written to the bridge's stdin. A value that cannot be written as JSON stops it before the write; a write "
            "or flush that fails on the pipe can stop it partway, so the bridge may have received part of the "
            "request."
        ),
        remediation=(
            "Read `stderr_tail`: a bridge that crashed or closed its input usually says why in its last lines, and "
            "that is the cause to fix.",
            "The refusal carries `side_effect_status: unknown` because the write may have stopped partway, and on a "
            "send part of the frame request may have reached the bridge. On a send or read the lease is then "
            "quarantined, so the next call on this bus answers `resource_quarantined`.",
            "`can_session_stop` then answers `can_adapter_close_failed`, because a quarantined lease does not "
            "release; check the bench and follow the sign-off under `resource_quarantined`.",
        ),
        do_not=(
            "Do not resend on the assumption that the bridge received none of it. Part of the request may have "
            "reached it.",
        ),
    ),
    "can_adapter_not_found": ErrorRemedy(
        meaning=(
            "The CAN bus is configured `adapter: process`, and the bridge it names is not a file at the resolved "
            "path: nothing is there, or the path is a directory or a dangling link. No process was started."
        ),
        remediation=(
            "Check `can_buses.<id>.executable`. A relative path is resolved against the workspace root "
            "(`workspace_root`), not against the directory the server was started from.",
            "Fix the path, or put the bridge where it points, then call `can_session_start` again.",
        ),
        do_not=(
            "Do not point `executable` at a program without checking that it is the bridge for this adapter: "
            "whatever it names is started and spoken to as one.",
        ),
    ),
    "can_adapter_open_failed": ErrorRemedy(
        meaning=(
            "The CAN adapter would not open. For a direct adapter the python-can bus constructor raised; for a "
            "process bridge, the bridge answered its `open` with this refusal."
        ),
        remediation=(
            "Read `backend_error`, the driver's or the bridge's own reason: a missing device, a busy channel, a bus "
            "parameter the adapter rejected.",
            "Match it against `likely_causes`. If another program holds the adapter (a vendor tool, another server, a "
            "script), close that program first; most drivers hand out one handle per channel.",
            "Once the cause is fixed, call `can_session_start` again. When the refusal also carries `cleanup_error`, "
            "the bridge would not close either and stays registered, and the entry for that type comes first.",
        ),
        do_not=(
            "Do not open the channel from a python-can script to see whether it works while a start is pending. The "
            "next start then fails on the handle that script holds.",
        ),
    ),
    "can_adapter_process_exited": ErrorRemedy(
        meaning=(
            "The CAN process bridge was not running when this server came to write a request to it, so that request "
            "was not written: the bridge exited or was killed, during its own `open` or after it, or this session "
            "had already ended it. What it last did on the bus before it went is unknown."
        ),
        remediation=(
            "Read `stderr_tail`: the bridge's last output usually says why it exited, an exception, a driver error or "
            "a device that went away.",
            "A bridge that has exited cannot confirm a safe state any more, so `can_session_stop` answers "
            "`can_adapter_close_failed` once, records the unconfirmed close and gives the bus back.",
            "Fix what made it exit and check the bench by hand, then `can_session_start` opens a fresh bridge.",
        ),
        do_not=(
            "Do not start the bridge by hand to take the bus back. Until `can_session_stop` ends the session this "
            "server still holds the bus for it, and a bridge on the channel outside a session is outside every "
            "record.",
        ),
    ),
    "can_adapter_process_start_failed": ErrorRemedy(
        meaning=(
            "The CAN process bridge configured for this bus is a file, and the operating system would not start it. "
            "Nothing ran, so the bus was not touched."
        ),
        remediation=(
            "Read `backend_error`: a permission error means the file may not be executed, an exec-format error that "
            "the system does not know how to run it, and a missing file usually means its interpreter is missing.",
            "A `.py` bridge is run with this server's own Python interpreter and needs no executable bit. Any other "
            "file is run directly, and needs the executable bit and, for a script, an interpreter line.",
            "It is started in its own directory, the one holding `can_buses.<id>.executable`; check that the "
            "directory still exists and this account may enter it.",
        ),
        do_not=(
            "Do not wrap the bridge in a shell command line to get it started. `executable` is one file path, and "
            "nothing in it is handed to a shell.",
        ),
    ),
    "can_adapter_timeout": ErrorRemedy(
        meaning=(
            "The CAN process bridge did not answer a request in the time it was given. The request had been written, "
            "so whether the bridge acted on it is unknown, and on a send whether the frame reached the bus is unknown "
            "too."
        ),
        remediation=(
            "Read `side_effect_status`: `unknown` means the bridge had the request and did not say what it did, so on "
            "a send the frame may be on the bus.",
            "`stderr_tail` holds the bridge's last output, often where it is blocked: its driver, a full transmit "
            "queue, a controller in bus-off.",
            "On a send or read the lease is quarantined, so the next call on this bus answers `resource_quarantined`; "
            "check the bus from the target's side and follow that entry.",
            "If the bridge is only slow to open or to send, raise `can_buses.<id>.timeout_s`, the time those requests "
            "are given; a read is given the shorter of its own wait and that time, plus a second, and a close one second.",
        ),
        do_not=(
            "Do not send the frame again to find out whether the first one went out. A duplicate stimulus on a live "
            "bus is the outcome the unknown status exists to prevent.",
        ),
    ),
    "can_backend_not_available": ErrorRemedy(
        meaning=(
            "python-can could not be imported by the interpreter running this server, so a direct adapter "
            "(`socketcan` or `peak`) cannot be opened. A `process` bridge is a separate program that imports what it "
            "needs itself, so this server needs python-can only for a direct adapter."
        ),
        remediation=(
            "Read `backend_error`: `ModuleNotFoundError` means python-can is not installed there, and any other "
            "import error means it is installed and fails while loading, often on a vendor library.",
            "Install `agentic-hil[can]` into the environment the MCP server runs from, with that environment's own "
            "Python, then call `can_session_start` again; restart the server if the import still fails.",
        ),
        do_not=(
            "Do not install python-can into a different Python than the one the MCP client starts. The install looks "
            "done and the server keeps failing the same way.",
        ),
    ),
    "can_broker_authentication_failed": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus refused this client's authentication key. The broker writes its key "
            "beside its descriptor in the lock directory and every participant reads it from there, so a refusal "
            "means the key this client read is not the one the broker holds: the broker was replaced between the "
            "two reads, or the file changed after the broker wrote it."
        ),
        remediation=(
            "Read `backend_error`, the connection library's own reason. `retry_safe` is false, so the attach was not "
            "retried.",
            "Call `can_session_start` once more: it reads the descriptor and the key afresh, which settles a broker "
            "that was replaced between the two reads.",
            "If it repeats, stop every participant on the bus with `can_session_stop` so the broker exits; the next "
            "start begins a broker with a new key.",
        ),
        do_not=(
            "Do not copy, edit or replace the key file to make the two agree. The key is how the broker tells its "
            "participants from any other local process.",
        ),
    ),
    "can_broker_counter_mismatch": ErrorRemedy(
        meaning=(
            "The broker's connection counter had moved on since this client read the broker descriptor: another "
            "participant attached in between. The client retries this a fixed number of times and then until its "
            "start deadline, so reaching the caller means it was already retried until the deadline with attaches "
            "landing all along."
        ),
        remediation=(
            "Compare `broker_counter` with `client_counter`: a gap means attaches landed between this client's read "
            "of the descriptor and its own attach.",
            "`retry_safe` is true, and the attach was already retried until the deadline; call `can_session_start` "
            "again once the other participants' starts have settled.",
        ),
        do_not=(
            "Do not start participants on this bus in a tight loop from several runs at once. Every attach moves the "
            "counter the others are waiting on.",
        ),
    ),
    "can_broker_disconnected": ErrorRemedy(
        meaning=(
            "The connection between this participant and the broker for this shared CAN bus ended in the middle of a "
            "request: the broker process has exited, or it closed this participant's connection. It is a connection "
            "failure and nothing else; no audit record failed, because the trail is not written through that pipe."
        ),
        remediation=(
            "Read `backend_error`, the pipe's own error, and `side_effect_status`: `unknown` on `can_send` means the "
            "request may have reached the broker before it ended, so the frame may be on the bus; `not_started` on "
            "`can_read` means the read put nothing on the bus.",
            "After an unknown send the lease is quarantined, so the next call on this participant answers "
            "`resource_quarantined`; check the bus from the target's side and follow that entry.",
            "This session puts no further request to the broker: until it is stopped, a later `can_send` or "
            "`can_read` on it answers this failure, or `resource_quarantined` after an unknown send. Stop it with "
            "`can_session_stop` and start it again with `can_session_start`, which starts a fresh broker when the old "
            "one has exited.",
        ),
        do_not=(
            "Do not send the frame again to find out whether the first one went out. A duplicate stimulus on a live "
            "bus is the outcome the unknown status exists to prevent.",
        ),
    ),
    "can_broker_invalid_message": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus and this client could not read each other: the broker received a "
            "message it cannot parse, or it answered the attach, a send or a read with something that is not an "
            "answer. Both ends are code from this package, so this is a fault in the broker connection rather than "
            "on the bus."
        ),
        remediation=(
            "Read `summary`: it says which end could not read the other.",
            "On `can_send`, `side_effect_status` is `unknown`: the broker had the request, so the frame may be on the "
            "bus, and the lease is quarantined. On `can_read` it is `not_started`. Either way this session puts no "
            "further request to the broker until it is stopped.",
            "The broker keeps running while any participant is attached, so it is replaced only after every "
            "participant has detached and it exits; stop the others with `can_session_stop`, then call "
            "`can_session_start` again.",
        ),
        do_not=(
            "Do not end the broker process by hand while participants are attached. Each of them loses its run with "
            "no incident on record.",
        ),
    ),
    "can_broker_not_bus_owner": ErrorRemedy(
        meaning=(
            "The broker named by the descriptor for this shared CAN bus does not hold the bus lock it claims to own, "
            "so the client would not talk to it. Either the lock is free and the descriptor is left over from a "
            "broker that ended without cleaning up, or the lock is held with no holder record, or it is held by a "
            "different owner than the descriptor names."
        ),
        remediation=(
            "Read `bus_lock_held`: false means the bus lock is free and the descriptor is stale, which a start that "
            "may launch a broker clears away by itself; true means a process holds the lock.",
            "`bus_lock_holder`, when present, names the owner, process and host holding the lock, and "
            "`claimed_broker_pid` the process the descriptor names. The bus lock is the same one a single-owner "
            "`can_session_start` takes, so the holder may be a session opened without a participant, or another "
            "server.",
            "Stop that holder's session with `can_session_stop` where it runs, then call `can_session_start` again. "
            "With the lock held and no holder named, `retry_safe` was true and the attach was already retried until "
            "its deadline, so the holder never identified itself.",
        ),
        do_not=(
            "Do not delete the descriptor, the lock or the holder record by hand. A held lock belongs to a live "
            "process, and taking its files away lets a second owner onto the same bus.",
        ),
    ),
    "can_broker_protocol_mismatch": ErrorRemedy(
        meaning=(
            "The broker running for this shared CAN bus and this client speak a different version of the broker "
            "protocol, so they cannot be attached. Usually one server was upgraded while a broker started by an "
            "older one still serves its participants."
        ),
        remediation=(
            "Compare `broker_protocol_digest` with `client_protocol_digest` and the protocol versions beside them: "
            "the client's are this server's, and the broker was started by a server on another release.",
            "Let the old broker exit: stop the participants attached to it with `can_session_stop` in the servers "
            "that started them, or restart those servers, then call `can_session_start` again so a broker of this "
            "release starts.",
        ),
        do_not=(
            "Do not downgrade this server to match the old broker. The next fresh broker would mismatch the other "
            "way, and both servers would be on a release nobody chose.",
        ),
    ),
    "can_broker_stopping": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus was shutting down when the attach reached it, because its last "
            "participant had just detached, and it accepts no new participant. The client retries until its start "
            "deadline, expecting to find the old broker gone and a fresh broker started, so reaching the caller "
            "means the old broker's shutdown outlasted that deadline."
        ),
        remediation=(
            "`retry_safe` is true and the attach was retried until the start deadline: the old broker was still "
            "shutting down for that whole time.",
            "Call `can_session_start` again; once the old broker has exited, a fresh broker is started for the bus.",
        ),
        do_not=(
            "Do not end the stopping broker by hand. It is closing the adapter, and cutting that short leaves the "
            "bus in whatever state the close had reached.",
        ),
    ),
    "can_broker_timeout": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus did not answer a request of this participant within the client's "
            "request timeout of 30 seconds. The request had been written, so whether the broker acted on it is "
            "unknown, and on a send whether the frame reached the bus is unknown too. A late answer would be read as "
            "the answer to the next request, so this session puts no further request to the broker."
        ),
        remediation=(
            "Read `side_effect_status`: `unknown` on `can_send` means the frame may be on the bus and the lease is "
            "quarantined, so the next call on this participant answers `resource_quarantined`; `not_started` on "
            "`can_read` means the read put nothing on the bus.",
            "Until the session is stopped, a later `can_send` or `can_read` on it answers this failure, or "
            "`resource_quarantined` after an unknown send. Stop it with `can_session_stop` and start it again with "
            "`can_session_start`.",
            "A read waits in the broker for the shorter of its `wait_timeout_s`, `can_buses.<id>.timeout_s` and 60 "
            "seconds, so a read asked to wait 30 seconds or longer can outlast the client; keep the wait shorter.",
        ),
        do_not=(
            "Do not send the frame again to find out whether the first one went out. A duplicate stimulus on a live "
            "bus is the outcome the unknown status exists to prevent.",
        ),
    ),
    "can_broker_unavailable": ErrorRemedy(
        meaning=(
            "No broker for this shared CAN bus could be reached or started in time. The client retries every outcome "
            "that is safe to retry until its start deadline, so this refusal has already been retried until that "
            "deadline."
        ),
        remediation=(
            "Read `summary`: it says which step failed (an endpoint that could not be reached, no key beside the "
            "descriptor, a handshake that did not finish, or a started broker that never published), and "
            "`backend_error` carries the connection's own error when there was one.",
            "`broker_log`, when the refusal names it, holds every broker ever started for this bus, so only lines "
            "written after this start can belong to this attempt, and there may be none.",
            "When `broker_start_timeout_s` is shorter than `bus_timeout_s`, the broker may still have been opening "
            "its adapter when the client gave up and ended it; a slow adapter open is the usual cause.",
            "Call `can_session_start` again only after something changed; the same attempt already ran to its "
            "deadline.",
        ),
        do_not=(
            "Do not delete the descriptor or the bus lock to force a new broker. A client clears a stale descriptor "
            "itself when the lock behind it is free, and a held lock belongs to a live process.",
        ),
    ),
    "can_broker_wrong_bus": ErrorRemedy(
        meaning=(
            "The broker endpoint this client reached owns a different bus than the one it asked for. A bus is "
            "identified by its adapter and channel, so the descriptor this client read led to a broker for another "
            "bus: the configuration of this bus changed after one side loaded it, or the descriptor is not that "
            "broker's own."
        ),
        remediation=(
            "Compare `broker_bus_key` with `client_bus_key`: the first is the bus the broker serves, the second the "
            "one this server derived from its configuration.",
            "`retry_safe` is false and the attach was not retried. Check that the configuration this server loaded "
            "still names the adapter and channel the bench uses, then call `can_session_start` once more.",
        ),
        do_not=(
            "Do not edit `channel` on an entry only to make the two keys agree. The key follows the physical bus, "
            "and a channel spelled to match is a claim on a medium the entry does not describe.",
        ),
    ),
    "can_bus_gated": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus refused the attach because an earlier bus-scoped incident gated the "
            "bus. The gate lasts as long as this broker: it accepts no new participant until every participant has "
            "detached and it exits, and a fresh broker starts ungated."
        ),
        remediation=(
            "Read `incident`: its `reason` and `detail` say what failed on the adapter when the bus was gated.",
            "Check the bench for that cause, then stop every participant still attached to this bus with "
            "`can_session_stop`, in the servers that hold them.",
            "Once the last one has detached the broker exits, and `can_session_start` then starts a fresh broker that "
            "is not gated.",
        ),
        do_not=(
            "Do not keep calling `can_session_start` while participants stay attached. The gate holds for the life of "
            "the broker, and every attach is answered the same.",
        ),
    ),
    "can_bus_incident": ErrorRemedy(
        meaning=(
            "A fault on this shared CAN bus aborted the run of every participant attached to it, and the broker gated "
            "the bus: it accepts no new participant and answers each later call of an aborted participant with this "
            "refusal. The fault was the adapter's send or read raising or failing, or one a participant reported."
        ),
        remediation=(
            "Read `abort`: its `reason` (`can_adapter_send_raised`, `can_adapter_send_failed`, "
            "`can_adapter_read_raised`, `can_adapter_read_failed`, `can_adapter_invalid_response`, or one a "
            "participant reported) and `detail` say what failed on the adapter.",
            "`aborted_participants` lists every run aborted with this one, and `bus_gated` is true: the bus is not "
            "running for anyone until the broker exits.",
            "This server quarantined its lease on the bus, so its next call answers `resource_quarantined` and "
            "`can_session_stop` answers `can_adapter_close_failed` while the quarantine stands. Check the bench, "
            "then follow the sign-off under `resource_quarantined`.",
        ),
        do_not=(
            "Do not start a new participant on this bus to carry on the test. The broker refuses it with "
            "`can_bus_gated` for as long as it runs.",
        ),
    ),
    "can_bus_not_configured": ErrorRemedy(
        meaning=(
            "The `bus_id` is not a key of `can_buses` in the authoritative configuration this server loaded, so "
            "nothing was opened or sent."
        ),
        remediation=(
            "Pick an id from `configured_buses` beside this refusal: those are exactly the buses this server knows.",
            "`can_buses_list` shows each of them with its adapter, channel and shares. A bus that should exist has "
            "to be declared under `can_buses` in the authoritative configuration.",
        ),
        do_not=(
            "Do not pass the adapter's channel name (`can0`, `PCAN_USBBUS1`) as the id. The id is the configuration's "
            "key, and the channel is a field inside it.",
        ),
    ),
    "can_bus_not_shared": ErrorRemedy(
        meaning=(
            "A participant session was asked for on a CAN bus that declares no `shares:`, which makes it a "
            "single-owner bus without participants. When `can_session_start` named a participant this server did "
            "find configured, the refusal comes from the broker, which loads the configuration itself when it "
            "starts: the file was edited after this server loaded it, and the broker read the bus without shares."
        ),
        remediation=(
            "Check the bus in `can_buses_list`: a bus without `shares` is opened by one session, so call "
            "`can_session_start` without `participant`.",
            "If the bus should be shared, compare the configuration file with what `can_buses_list` shows: an entry "
            "changed since the server loaded it is not the one this call was checked against, and restarting the "
            "server brings the two together again.",
        ),
        do_not=(
            "Do not add a `shares:` block only to get past this. Sharing decides who may transmit on the bus, and "
            "is declared for that reason.",
        ),
    ),
    "can_listen_only_conflict": ErrorRemedy(
        meaning=(
            "The broker refused the attach because listen-only belongs to the whole bus: a participant that requires "
            "a silent bus and one that may transmit cannot be attached together, and a participant that may "
            "transmit cannot attach to a bus configured `listen_only: true`."
        ),
        remediation=(
            "Read `conflicting_participants`: empty means the bus itself is `listen_only` and this participant may "
            "transmit; otherwise it lists the attached participants this one conflicts with.",
            "Run the two kinds one after the other: stop the conflicting ones with `can_session_stop` first. A "
            "share's `requires_listen_only` and its `permissions.allow_write` decide which kind it is.",
            "A participant that never transmits does not need `permissions.allow_write`; without it, it attaches "
            "beside participants that require a silent bus.",
        ),
        do_not=(
            "Do not turn off the bus's `listen_only` to let a participant that transmits onto it. A bus configured "
            "silent is a claim about the medium that the bench and the other runs rely on.",
        ),
    ),
    "can_participant_busy": ErrorRemedy(
        meaning=(
            "The broker for this shared CAN bus already has a participant of this name attached, and it seats one "
            "attach per name. The client retried until its start deadline, and the name stayed attached for that "
            "whole time."
        ),
        remediation=(
            "`can_buses_list` shows this server's `active_participants` on the bus; if the name is there, end that "
            "session with `can_session_stop` first.",
            "Otherwise the attach belongs to a session outside this server, or to one that ended without detaching. "
            "`retry_safe` is true and the attach was already retried until the deadline, so call "
            "`can_session_start` again once the broker has let the name go, and check `agentic-hil lease-status` "
            "for who holds the participant if it does not.",
        ),
        do_not=(
            "Do not use one participant name from two runs. A name is one view with one frame budget, and a second "
            "attach is refused for that reason.",
        ),
    ),
    "can_participant_filter_violation": ErrorRemedy(
        meaning=(
            "The frame's identifier is outside this participant's view of the shared CAN bus, so the broker would not "
            "transmit it: it was not sent. A view matches the identifier and its format together, so an extended "
            "identifier falls outside a filter written for the standard identifier with the same number."
        ),
        remediation=(
            "Compare `frame` with `view`: `view` holds the participant's filter, frame budget and permissions, and "
            "`frame` the identifier and format that fell outside it.",
            "Check `extended` on the frame; a filter term matches only frames of its own format.",
            "If the participant should send it, widen the filter in `can_buses.<id>.shares`. A running broker keeps "
            "the configuration it started with, so the change applies to the next broker.",
        ),
        do_not=(
            "Do not send the frame through another participant whose view happens to allow it. The view is what "
            "this test is permitted to put on the bus.",
        ),
    ),
    "can_participant_frame_budget_exhausted": ErrorRemedy(
        meaning=(
            "This participant used the whole frame budget of its share on the shared CAN bus, so the broker aborted "
            "its run. Only this participant is affected: the bus keeps running for the others."
        ),
        remediation=(
            "Compare `max_frames` with `frames_used`; from here every call of this participant answers "
            "`can_participant_incident`.",
            "Stop the participant with `can_session_stop` and attach it again with `can_session_start`: a fresh "
            "attach counts its frames from zero.",
            "If the test needs more frames, raise `max_frames` on the share in `can_buses.<id>.shares`; a broker "
            "reads it when it starts.",
        ),
        do_not=(
            "Do not split one test across several participant names to get more frames. The budget is how much one "
            "test may put on the bus.",
        ),
    ),
    "can_participant_incident": ErrorRemedy(
        meaning=(
            "This participant's run on the shared CAN bus was aborted by an incident scoped to it alone: it used up "
            "its frame budget, its receive queue overflowed, or it reported an incident of its own. Only this "
            "participant was aborted; the bus was not gated and the other participants were not touched."
        ),
        remediation=(
            "Read `abort`: its `reason` is `can_participant_frame_budget_exhausted`, "
            "`can_participant_receive_overflow` or one the participant reported, and `detail` holds the limits it ran "
            "into or what the participant reported.",
            "Stop this participant with `can_session_stop` and attach it again with `can_session_start`; the abort "
            "belongs to this attach, and a fresh one starts clean.",
            "For an overflow, read more often with `can_read`, or narrow the share's `filter` so fewer frames queue "
            "for it.",
        ),
        do_not=(
            "Do not stop the other participants on the bus over this. Their runs were not aborted, and the bus is "
            "still carrying their traffic.",
        ),
    ),
    "can_participant_lock_required": ErrorRemedy(
        meaning=(
            "This server took the lease for a participant and, attaching it, found that its bench mutex does not "
            "hold that participant's lock. The lease and the lock are taken together, so this is the server's own "
            "bookkeeping disagreeing with itself, not another run holding the name."
        ),
        remediation=(
            "`participant_lock` names the lock that was expected and not held. `retry_safe` is true: call "
            "`can_session_start` again, which takes the lease and the lock afresh.",
            "If it repeats, check `agentic-hil lease-status` and restart the MCP server: its bench mutex and its "
            "leases no longer agree, and a fresh server takes both anew.",
        ),
        do_not=(
            "Do not take the participant lock by hand or from a second process to satisfy the check. The lock is "
            "what keeps two runs off one participant name.",
        ),
    ),
    "can_participant_not_configured": ErrorRemedy(
        meaning=(
            "The participant named is not a share of this CAN bus. From this server it means the authoritative "
            "configuration declares no such share; from the broker, that the configuration the broker loaded when it "
            "started declares none, which differs from this server's after an edit."
        ),
        remediation=(
            "Pick a name from `configured_participants`, the shares declared on this bus.",
            "An empty `configured_participants` means the bus declares no `shares:` and has one owner: call the tool "
            "again without `participant`.",
            "If the name should exist, declare it under `can_buses.<id>.shares`. A running broker keeps the "
            "configuration it started with, so a new share is seen once every participant has detached and a fresh "
            "broker starts.",
        ),
        do_not=(
            "Do not borrow another participant's name because it is configured. Its view, budget and permissions "
            "belong to that participant, and two runs cannot share one.",
        ),
    ),
    "can_participant_required": ErrorRemedy(
        meaning=(
            "This CAN bus declares `shares:`, so it is shared through a broker and every call on it has to name the "
            "participant it acts as. The call named none and was refused before anything was opened or sent."
        ),
        remediation=(
            "Pick one of `configured_participants` and pass it as `participant` to `can_session_start`, `can_send`, "
            "`can_read` and `can_session_stop` alike.",
            "`can_buses_list` shows each share's view (its `filter`, `max_frames` and permissions), which tells the "
            "one this test should use.",
        ),
        do_not=(
            "Do not remove `shares:` from the bus to use it without a participant. Other runs sharing the bus would "
            "lose the views that keep their traffic apart.",
        ),
    ),
    "can_queue_clear_failed": ErrorRemedy(
        meaning=(
            "`can_session_start` was clearing the receive queue (`clear_rx_queue: true`, the default) and a read "
            "during the drain failed, answered in a shape that is not a drain, or could not be audited. A session "
            "this call had just opened was closed again; one that was already running stays open."
        ),
        remediation=(
            "Read `backend_result`, the adapter's own answer to the failed read, and `frames_drained`: the frames "
            "already read off the queue were discarded and cannot be read again.",
            "`retry_safe` is true only when nothing was drained; then call `can_session_start` again.",
            "If the drain keeps failing, start with `clear_rx_queue: false` and read what is queued with `can_read`, "
            "which reports a failing read as its own refusal.",
        ),
        do_not=(
            "Do not assume the queue is empty after this refusal. It was not cleared, and frames from before the "
            "start may still be read as if they were new.",
        ),
    ),
    "can_queue_clear_limit": ErrorRemedy(
        meaning=(
            "The receive queue did not become empty within the bounded drain `can_session_start` runs before it "
            "reports a session, a fixed number of reads within about a second: frames arrived as fast as they were "
            "read. The bus is busy, which is a fact about the bus and not a fault."
        ),
        remediation=(
            "`frames_drained` is how many frames were read and discarded before the limit. A session this call had "
            "just opened was closed again.",
            "On a bus that never falls silent, start with `clear_rx_queue: false`, then read with `can_read` and "
            "`until_id` to stop at the frame the test waits for.",
        ),
        do_not=(
            "Do not call `can_session_start` again and again waiting for a quiet moment. A bus with continuous "
            "traffic gives none, and every attempt discards another batch of frames.",
        ),
    ),
    "can_read_failed": ErrorRemedy(
        meaning=(
            "The direct CAN adapter's receive raised during `can_read`. A receive transmits nothing, so nothing was "
            "sent and the read may be repeated; what failed is the adapter or its link."
        ),
        remediation=(
            "Read `backend_error`, the driver's own error: a link that went down, an adapter unplugged or reset, a "
            "controller in bus-off.",
            "When `retry_safe` is true, call `can_read` again once the link is back. A link that stays down fails "
            "every read the same way; `can_session_stop` and then `can_session_start` reopens the adapter.",
        ),
        do_not=(
            "Do not take this refusal as a silent bus. A failed read says nothing about the traffic on it.",
        ),
    ),
    "can_send_failed": ErrorRemedy(
        meaning=(
            "The CAN adapter did not report the frame as sent. Whether it reached the bus depends on where the send "
            "failed, and `side_effect_status` says what is known: the adapter may have handed it to the controller "
            "before failing."
        ),
        remediation=(
            "Read `side_effect_status`. Only when it is `not_started` did nothing reach the bus, and only then is it "
            "safe to send the frame again.",
            "When it is `unknown`, the frame may be on the wire: check the bus from the target before sending "
            "anything else, and expect the lease to be quarantined, so the next call answers `resource_quarantined`.",
            "`backend_error` is the driver's own reason: a link that went down, no other node acknowledging the "
            "frame, a controller in bus-off.",
        ),
        do_not=(
            "Do not resend on the assumption that a failed send transmitted nothing. With `unknown`, a duplicate "
            "stimulus on a live bus is the risk.",
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


def remediation_fields(error_type: str | None, scope: str | None = None, *, permission: str | None = None) -> JsonObject:
    """The remediation fields for an error, or an empty object when none is known.

    Merge the result into a failing payload. Empty for every error_type the
    catalogue does not cover, so callers can apply it unconditionally without
    inventing advice for errors nobody has written a fix for.

    ``permission`` is the one substitution the catalogue cannot supply itself:
    the dotted key a `permission_denied` is about is a fact about the refusal in
    hand. Given, it fills the `{permission}` placeholder in that entry's steps,
    so the advice names the key the operator has to move; left out, the generic
    shape stands and the entry still reads (#443).
    """
    remedy = lookup_remedy(error_type, scope)
    if remedy is None or (permission is None and _needs_a_permission_key(remedy)):
        return {}
    values = {**_substitutions(), **({"permission": permission} if permission else {})}
    payload: JsonObject = {"remediation": [step.format(**values) for step in remedy.remediation]}
    if remedy.do_not:
        payload["do_not"] = [step.format(**values) for step in remedy.do_not]
    return payload


def run_remediation_fields(run_error: object) -> JsonObject:
    """The advice for the error a plan run publishes, whatever that error is.

    Looked up by the published type itself, under `TEST_REACTOR_SCOPE` with the
    bare entry behind it, rather than from a list of the types a run is known to
    fail with: a run passes up whatever its failing step answered, a debug
    session's target type included, and a list would leave every type it did
    not name without the fix its entry already holds. Empty for a value that is
    not a type, and for a type the catalogue has no entry for."""
    if not isinstance(run_error, str) or not run_error:
        return {}
    return remediation_fields(run_error, TEST_REACTOR_SCOPE)


def with_run_remediation(result: JsonObject) -> JsonObject:
    """`result`, a failed run's answer, with the advice for the error it publishes.

    A result that carries advice already keeps it: that advice was chosen where
    more was known than the type, such as the scope a coordinator refusal was
    answered under or the permission key a preflight refusal names. Filled in
    place and returned, so a caller can wrap the answer it is about to hand
    back."""
    if result.get("ok") is False and "remediation" not in result:
        result.update(run_remediation_fields(result.get("error_type")))
    return result


def _needs_a_permission_key(remedy: ErrorRemedy) -> bool:
    """Whether this entry's advice is about one named key and nothing else.

    `permission_denied` is the one error_type two unlike refusals share. Most of
    them are a key that is closed, and the entry tells the operator which key to
    move. A few are not a key at all: a symbol outside `debug.allowed_symbols`,
    a dump over `debug.max_dump_size_bytes`. Handing those the keyed entry would
    tell an operator to grant a permission that has nothing to do with the
    refusal, so an entry whose steps are written around `{permission}` answers
    only for a refusal that supplies one. `catalogue_entry` is unaffected: a
    reader browsing the error reference has met no refusal, and the generic
    shape is what they are there to read.
    """
    return any("{permission}" in step for step in (*remedy.remediation, *remedy.do_not))


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
        physical_check="A successful debug_halt clears this without an operator only while the session status is not error; if the session is in error, stop it with debug_stop_session before starting another session. Otherwise observe the board (heartbeat LED, console output) to see whether firmware runs, reset it by its own controls, then sign.",
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
    "serial_write_incomplete": QuarantineReasonGuide(
        attempted="com_write sent a payload of `bytes_requested` bytes, and the line took fewer of them.",
        confirmed="`bytes_written` bytes reached the line and the rest never left the host: every write returned normally, and the COM log records exactly the bytes that were sent. Nothing is quarantined and the session stays usable.",
        unknown="What the target made of a partial message: whether it ignored it, is waiting for the rest, or acted on what arrived.",
        physical_check="No signature is owed and `agentic-hil recover` has nothing to settle. Call `com_read` to see how the target took the partial message, then send the missing bytes or bring the target to a known state by its own protocol before the next command.",
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
    "can_participant_attach_unconfirmed": QuarantineReasonGuide(
        attempted="Attaching a named CAN participant to the shared broker did not return a confirmed result.",
        confirmed="The attach path sends no CAN frame and the participant session was not admitted for use.",
        unknown="Whether the broker accepted the participant connection before its reply was lost.",
        physical_check="Confirm the broker has no active connection for this participant, the adapter remains under the broker, and bus traffic is normal, then sign.",
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
        "gdb_server_executable": {"status": "ignored", "note": "OpenOCD is its own GDB server: a typed debug session runs `executable` with a `gdb_port` on a port this server reserves."},
        "probe_id": {"status": "optional", "note": "Adapter serial number. OpenOCD 0.12.0 and newer are passed `adapter serial <probe_id>`; an older release is passed the adapter driver's own serial command (`hla_serial`, `st-link serial` or `cmsis_dap_serial`), and a call whose driver has none is refused `not_supported` before OpenOCD is started for it. Required once more than one debugger is configured."},
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
        "gdb_server_executable": {"status": "discovered", "note": "The GDB server typed debug sessions run: ST-LINK_gdbserver, which STM32CubeCLT installs beside STM32_Programmer_CLI because the CLI has none. Falls back to the one in the same STM32CubeCLT tree as `executable` (`STLink-gdb-server/bin` beside `STM32CubeProgrammer/bin`), then ST-LINK_gdbserver on PATH, then the STM32CubeCLT installations under C:/ST. Held to the rules `executable` is. Started with `-cp` naming the CLI's directory, `-i <probe_id>`, `-d` for SWD and `-g`, so the connect neither resets nor moves the core. Unset and not found, flashing, probing and reading are unchanged and the debug session tools are refused naming this key."},
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
        "executable": {"status": "discovered", "note": "Falls back to `pyocd` on PATH. Install with `pip install agentic-hil[pyocd]` or `pip install pyocd`. Typed debug sessions run `pyocd gdbserver` from the same executable, on a port this server reserves for the session, with the same `--uid`, `--target` and `-W` every other call carries and pyOCD's semihosting console switched off, so the server opens no port of its own beside the one GDB connects to."},
        "gdb_server_executable": {"status": "ignored", "note": "Typed debug sessions run `pyocd gdbserver` from `executable`."},
        "probe_id": {"status": "optional", "note": "Probe unique ID, passed as `--uid`. pyOCD matches it as a case-insensitive substring and strips a leading `<type>:`, so give the full ID. Required once more than one debugger is configured."},
        "target_type": {"status": "required", "note": "Passed as `--target`. Omitted entirely when unset, leaving pyOCD to guess from the probe's board ID. Most vendor parts resolve only after a CMSIS pack is installed."},
        "interface": {"status": "ignored"},
        "interface_cfg": {"status": "ignored"},
        "target_cfg": {"status": "ignored"},
        "connect_mode": {"status": "refused", "default": "hotplug", "enum": ["hotplug"], "note": "Nothing this key could say reaches pyOCD, so `under_reset` is refused at load rather than accepted and ignored. A bench that needs the flash to connect under reset runs it on `type: stlink`. The one connect option this server does pass to pyOCD is not this key's: the typed-debug memory reads with no session open send `--connect attach`, fixed, because it is the only mode pyOCD documents as reaching a running core without halting or resetting it, together with the target pack's DebugCoreStart sequence disabled, because that sequence lets a halted core run at the connect. `probe_target` keeps pyOCD's default connect, attach as well, and disables the same sequence. A typed debug session does not read this key either: whether it resets or attaches is the `mode` of `debug_start_session`, carried out by GDB against `pyocd gdbserver`."},
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
        "serial in its descriptor, which is the string OpenOCD selects the probe by. The toolchain is the "
        "`openocd` on PATH, and the generated entry is `type: openocd` with its interface_cfg and target_cfg. This is "
        "the path on an ordinary Linux workstation, which normally has OpenOCD and not STM32CubeProgrammer."
    ),
    "usb_serial_inventory_is_not_a_complete_count": (
        "This inventory reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone "
        "ST-LINK/V2 -- or any probe with no VCP -- can be attached and never appear: `complete: false`, an empty "
        "reading is not proof no probe is connected, and a sole visible one is not proof it is the only one. Exactly "
        "one visible ST-Link is bound anyway, because that is the ordinary OpenOCD-only bench and it has to reach a "
        "working configuration from `agentic-hil init` alone; what the incomplete count buys is a caveat rather than a "
        "refusal. The discovery result and the generated entry carry `discovered_by: usb_serial_inventory`, "
        "`probe_inventory: incomplete` and the sentence naming what this enumeration cannot see, and the `init` and "
        "`project_config_create` reports say it in a step of their own. Where a probe this inventory cannot reach is "
        "attached beside the visible one, name the intended board as probe_id (which `select_probe_id` still checks "
        "against the inventory, so a serial this host cannot see is refused `adapter_not_found` rather than added); "
        "where an authoritative count is what matters, install STM32CubeProgrammer, whose listing reads the serial off "
        "the probe itself. Two or more visible probes stay `ambiguous_hardware`. Only a reading that saw no ST-Link at "
        "all refuses `probe_inventory_incomplete`, because it has nothing to bind and cannot report an absent bench "
        "either: `project_config_create` writes nothing and `agentic-hil init` writes an unbound placeholder."
    ),
    "target_identity_without_the_cli": (
        "The workspace profile's `target.controller` when it names one, which is exact and says nothing to the board; "
        "otherwise a read-only OpenOCD `init`, `targets`, `shutdown` against the probe selected by that serial, which "
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
    "why": "the bound entry's name alone does not prove which physical probe a call reaches once another configured entry could just as easily be meant; probe_id is what pyOCD and ST-Link verify against the attached hardware, and the serial OpenOCD selects the probe by.",
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
    # of a line is read and what a session holds the target's modem lines at.
    # None of those is what an attached probe hands you either.
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
    # `gdb_server_executable` is `executable` again for the one backend whose
    # CLI has no GDB server: which program runs for a debug session on this
    # probe, a fact about the bench like the CLI beside it (#624).
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
    ConfigKeyRule("debuggers", named=True, under_permissions=False, right=CONFIG_DESCRIPTION_RIGHT, fields=("type", "probe_id", "executable", "gdb_server_executable", "interface_cfg", "target_cfg", "connect_mode")),
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
    "com_ports": "The serial lines. `device` is how a port is opened and `serial_number` is which board it is. Name both, because a kernel name like `/dev/ttyACM0` or `COM7` is an enumeration order and moves when another adapter is attached. `vid`/`pid` name which kind of adapter it is, which is what makes a serial mean a unit at all and is the only identity an adapter that publishes no serial can have. From `version: 3` on an entry must say which of them identifies it: a `serial_number`, a `resource_id` or a `/dev/serial/by-id/...` device name, or else an explicit `identity_source` (`vid_pid` for an adapter publishing USB ids but no serial, `device` for one publishing neither). Reading needs no permission; `assert_dtr`/`assert_rts` decide whether a session holds DTR and RTS asserted. On Linux the open itself was measured to pulse DTR once with `assert_dtr: false` (an FT232R: 239 to 943 microseconds per open), so a board that wires DTR to reset can still see the open.",
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

`config_status` compares the file against the effective description. If the file also changed a section this reload does not take, `restart_required_for` names those values and the status stays changed until a restart. When permissions differ from the ones being enforced, the status carries `permissions_source`, which says the grants came from the document parsed at startup. The reload's result lists changed grants under `permission_differences` and other deferred values under `restart_required_for`.

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

A written test plan needs no declaration around it. `test_reactor_run` drives the same reactor `agentic-hil test-reactor` drives, and there the plan *is* the declaration: every device it names is taken before its first step and held past its last, and a step reaching for one the plan did not name is refused with `undeclared_device` exactly as a call inside a `bench_run_start` would be. Asked for while this session's own `bench_run_start` is open, a plan is refused as `run_already_active` naming `bench_run_stop` as the way out: a plan is a run of its own, and one run per owner is the rule.

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

So an abandoned run costs nothing beyond the life of the server process. While it lasts, a contender's `device_busy` refusal carries `heartbeat_age_s` and, past four heartbeat intervals, `holder_heartbeat_stale: true`. An idle holder is visible rather than merely obstructive. A refusal that carries no `holder` carries no heartbeat either: the device is held by an owner whose record does not name it yet, so wait for it, bounded by `wait_s`, rather than reading it as a hang. Call `bench_run_status` if you are unsure whether you still hold the bench, and `bench_run_stop` to be sure you do not.

One case is outside this: a server process left running with its stdin never closed, by a host that leaked the pipe. It sees no disconnect and holds the run. Ending that process is the answer; never delete a lock file under `~/.agentic-hil/device-locks`.

### Two config entries, one board

The lock is keyed on the hardware, not on the name of the config entry. Two entries that describe one physical unit (a debug probe and its virtual COM port sharing a `resource_id`, or the same serial device configured twice) resolve to one lock key and are taken once. Declaring both is not an error and does not double-lock anything.

One serial device written down two ways is one port as well. An entry that names it through a link such as `/dev/serial/by-id/...` is also held under the node the link leads to, looked up each time the port is taken, and on Windows an entry naming `\\\\.\\COM7` is also held under `COM7`. A workspace that wrote the other spelling is refused with `device_busy`, naming the holder.

The one identity that is *not* hardware-derived: a debugger entry with neither `resource_id` nor `probe_id` falls back to the backend toolchain. Two boards driven by the same backend would then share one lock, and one board reached through two backends would take two. `bench_run_start` returns a `warnings` entry when a declared device is in that state; the fix is a `probe_id`, or a `resource_id` shared by every entry naming that unit.

Reading can still perturb a target: an SWD attach halts the core, a CAN controller outside `listen_only` sends dominant ACK bits, opening a serial port raises DTR on boards that wire it to reset. That is why the passive modes stay available: `can_buses.<name>.listen_only: true` is how a target is observed provably undisturbed, and `com_ports.<name>.assert_dtr: false` / `assert_rts: false` keep both lines released for the session. They are no longer a precondition for access. For CAN that is the way to prove a reading did not touch anything; a serial open is not proved untouched by them: on Linux, measured with an FT232R over 65 opens, the open itself asserted DTR once for 239 to 943 microseconds with `assert_dtr: false` before releasing it for the rest of the session and at close, so a board that wires DTR to reset sees that pulse.

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
PLAN_MINIMAL_EXAMPLE = r"""version: 6
name: boot-smoke
steps:
  - {device: dut, action: flash, image_path: build/app.elf, reset_after_flash: true}
  - {device: dut_uart, action: uart_open}
  - {device: dut_uart, action: uart_expect, text: "boot complete", timeout_s: 10}
"""

PLAN_COMPARATOR_EXAMPLE = r"""version: 6
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
        "Which of type, executable, gdb_server_executable, probe_id, target_type, interface, interface_cfg, target_cfg, connect_mode and flash_address each of openocd, stlink and pyocd requires, discovers, ignores, or refuses; when probe_id becomes mandatory; when flash_address is needed; which backend can connect under reset; and which of bootstrap discovery's two enumerations answers on a given host, which decides the type and executable a generated entry gets.",
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
