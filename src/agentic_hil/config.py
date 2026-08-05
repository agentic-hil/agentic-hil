from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, BinaryIO

import yaml
from jsonschema import Draft202012Validator, SchemaError
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from agentic_hil.knowledge import remediation_fields, safe_state_root_suggestion
from agentic_hil.types import (
    LEGACY_CONFIG_VERSION,
    READ_FREE_CONFIG_VERSION,
    AgenticHILConfig,
    ArtifactsConfig,
    CanBusConfig,
    ComPortConfig,
    DebuggerConfig,
    DebuggerPermissions,
    DebugInterfaceConfig,
    IoPermissions,
    JsonObject,
    LogsConfig,
    ProjectPermissions,
    RecoveryConfig,
    ReportsConfig,
    TargetConfig,
    ValidationConfig,
    fold_device_path,
    fold_hardware_id,
)

CONFIG_ENV = "AGENTIC_HIL_CONFIG"
CONFIG_DIGEST_ALGORITHM = "sha256"
CONFIG_SCHEMA_ID = "https://agentic-hil.local/schemas/config.schema.json"
CONFIG_SCHEMA_RESOURCE = "schemas/config.schema.json"
GDB_AUTODETECT_CANDIDATES = ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]
_PATH_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class ConfigError(Exception):
    def __init__(self, error_type: str, summary: str, details: JsonObject | None = None):
        super().__init__(summary)
        self.error_type = error_type
        self.summary = summary
        self.details = details or {}

    def to_dict(self) -> JsonObject:
        # Every CLI and tool path serializes a ConfigError here, so this is the
        # one place a concrete fix has to be attached. It comes from the same
        # catalogue the MCP reference serves, scoped by the config field the
        # error already names, so the fix in the refusal and the fix a caller
        # looks up cannot drift apart. Error types the catalogue does not cover
        # contribute nothing: no result grows advice nobody wrote.
        field = self.details.get("field")
        remediation = remediation_fields(self.error_type, field if isinstance(field, str) else None)
        return {"ok": False, "error_type": self.error_type, "summary": self.summary, **self.details, **remediation}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> JsonObject:
    loader.flatten_mapping(node)
    result: JsonObject = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a non-string mapping key",
                key_node.start_mark,
            )
        try:
            duplicate = key in result
        except TypeError as error:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from error
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


def config_digest(data: bytes) -> str:
    """A fingerprint of the exact bytes a configuration was parsed from.

    The content and nothing else. A modification time is the cheaper comparison
    and it is wrong in both directions here: a file rewritten with the same
    bytes — a `git checkout`, an editor's save-on-close, a re-run of `init` — did
    not change the policy and must not be reported as if it had, and two writes
    inside one timestamp tick are invisible to it, on filesystems whose
    granularity is a whole second or two. A hash answers the only question that
    matters, which is whether the document in force is still the document on
    disk."""
    return f"{CONFIG_DIGEST_ALGORITHM}:{hashlib.sha256(data).hexdigest()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_schema_text() -> str:
    return resources.files("agentic_hil").joinpath(CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8")


def config_schema() -> JsonObject:
    return json.loads(config_schema_text())


def validate_config_schema(raw: JsonObject, config_path: str | None = None) -> None:
    reject_nonfinite_numbers(raw, "config_invalid", config_path)
    schema = config_schema()
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        details: JsonObject = {"schema": CONFIG_SCHEMA_RESOURCE, "schema_error": str(error)}
        if config_path is not None:
            details["path"] = config_path
        raise ConfigError("config_schema_invalid", "Bundled Agentic HIL configuration schema is invalid.", details) from error

    errors = sorted(Draft202012Validator(schema).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        raise_config_validation_error(errors[0], config_path)


@dataclass(frozen=True)
class ValidatedDocument:
    """Everything a configuration document decides about itself.

    What ``load_config`` establishes before it looks at the machine: the checks
    that are a pure function of the document, kept together so the loader reads
    in one place and a reader of this class can see what a file alone decides."""

    config_version: int
    workspace_root: str
    target: TargetConfig
    debuggers: dict[str, DebuggerConfig]
    com_ports: dict[str, ComPortConfig]
    can_buses: dict[str, CanBusConfig]


def validate_config_document(raw: Any, config_path: str) -> ValidatedDocument:
    """Every load-blocking check that is a pure function of the document.

    The startup loader's first half. A schema-valid file can still be a file no
    server can load — ``workspace_root: relative``, two debuggers resolving to
    one probe, a ``resource_id`` spelled in two cases — and each of those is
    decidable from the text with no question put to the machine.

    Excluded, deliberately: everything that asks the machine rather than the
    document. Whether ``workspace_root`` exists, whether ``state_root`` can be
    created, the executables on disk. Those depend on the host and belong to
    ``load_config``.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config_invalid", "Agentic HIL configuration root must be a mapping.", {"path": config_path})
    reject_bridge_args(raw, config_path)
    reject_removed_sections(raw, config_path)
    reject_removed_keys(raw, config_path)
    reject_empty_allowed_roots(raw, config_path)
    config_version = configured_version(raw, config_path)
    reject_read_permissions(raw, config_path, config_version)
    validate_config_schema(raw, config_path)

    workspace_value = str(raw["workspace_root"])
    workspace_requested = Path(workspace_value).expanduser()
    if not workspace_requested.is_absolute():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an absolute path.",
            {"path": config_path, "field": "workspace_root", "value": workspace_value},
        )
    workspace = workspace_requested.resolve()
    # Lexical only: whether the directory is there and can be created is the
    # machine's answer and belongs to load_config.
    validated_state_root_paths(str(raw["state_root"]), workspace, config_path)

    target = target_config(mapping(raw.get("target"), "target"))
    debuggers = {name: named_debugger_config(name, value, target) for name, value in mapping(raw.get("debuggers"), "debuggers").items()}
    com_ports = {name: com_port_config(name, value) for name, value in mapping(raw.get("com_ports"), "com_ports").items()}
    can_buses = {name: can_bus_config(name, value) for name, value in mapping(raw.get("can_buses"), "can_buses").items()}
    # Before validate_debuggers, so a pair of debuggers differing only in the
    # case of their resource_id is named as the case collision it is instead of
    # as two probes that happen to resolve to one resource.
    validate_resource_ids(debuggers, com_ports, can_buses)
    validate_debuggers(debuggers)
    return ValidatedDocument(
        config_version=config_version,
        workspace_root=str(workspace),
        target=target,
        debuggers=debuggers,
        com_ports=com_ports,
        can_buses=can_buses,
    )


def load_config(config_path: str | None = None, work_dir: str | None = None) -> AgenticHILConfig:
    """Parse one config file. Production entrypoints must use load_authoritative_config."""
    if config_path is None:
        return load_authoritative_config(work_dir or Path.cwd())
    config_file = absolute_without_symlinks(Path(config_path).expanduser())
    resolved_config_path = str(config_file)

    loaded_bytes = b""
    try:
        loaded_bytes = safe_read_bytes(config_file)
        loaded = yaml.load(loaded_bytes.decode("utf-8"), Loader=UniqueKeyLoader)
    except FileNotFoundError as error:
        raise ConfigError(
            "config_file_not_found",
            "Agentic HIL configuration file could not be found.",
            {"path": resolved_config_path},
        ) from error
    except ConfigError as error:
        try:
            is_directory = stat.S_ISDIR(os.lstat(config_file).st_mode)
        except OSError:
            is_directory = False
        if is_directory:
            raise ConfigError(
                "config_unreadable",
                "Agentic HIL configuration file could not be read.",
                {"path": resolved_config_path},
            ) from error
        raise
    except OSError as error:
        raise ConfigError(
            "config_unreadable",
            "Agentic HIL configuration file could not be read.",
            {"path": resolved_config_path, "backend_error": str(error)},
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigError(
            "config_invalid",
            "Agentic HIL configuration file is not valid UTF-8 text.",
            {"path": resolved_config_path},
        ) from error
    except yaml.YAMLError as error:
        details: JsonObject = {"path": resolved_config_path}
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            details.update({"line": mark.line + 1, "column": mark.column + 1})
        raise ConfigError(
            "config_invalid",
            "Agentic HIL configuration file is not valid YAML.",
            details,
        ) from error

    raw: Any = loaded or {}
    document = validate_config_document(raw, resolved_config_path)

    workspace = Path(document.workspace_root)
    if not workspace.is_dir():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an existing directory.",
            {"path": resolved_config_path, "field": "workspace_root", "value": str(raw["workspace_root"])},
        )
    if work_dir is not None and workspace != Path(work_dir).resolve():
        raise ConfigError(
            "config_invalid",
            "Configured workspace_root does not match the requested work directory.",
            {"workspace_root": str(workspace), "expected_workspace": str(Path(work_dir).resolve())},
        )
    state_root = validated_state_root(str(raw["state_root"]), workspace, resolved_config_path)
    return assembled_config(raw, document, resolved_config_path, state_root, config_digest(loaded_bytes))


def assembled_config(raw: Any, document: ValidatedDocument, resolved_config_path: str, state_root: Path, digest: str) -> AgenticHILConfig:
    """The document and its already-validated roots as one ``AgenticHILConfig``.

    Split out of ``load_config`` so the assembly of the object is one step with a
    name. Assembles and validates nothing on its own; every check is either
    behind it in the document validation or ahead of it in the pinning.
    """
    debug_raw = mapping(raw.get("debug"), "debug")
    artifacts_raw = mapping(raw.get("artifacts"), "artifacts")
    validation_raw = mapping(raw.get("validation"), "validation")
    reports_raw = mapping(raw.get("reports"), "reports")
    logs_raw = mapping(raw.get("logs"), "logs")
    recovery_raw = mapping(raw.get("recovery"), "recovery")
    permissions_raw = mapping(raw.get("permissions"), "permissions")

    workspace = Path(document.workspace_root)
    target = document.target
    debuggers = document.debuggers
    # One configured probe has no ambiguity to resolve, so bind it and let the
    # single-board path stay free of a name it could only get wrong once.
    single = next(iter(debuggers.items())) if len(debuggers) == 1 else (None, None)
    # Auto-binding the only configured probe must honour its target override
    # exactly as bind_debugger() does, or the override would work on every
    # multi-probe project and be silently dropped on single-probe ones.
    if single[1] is not None and single[1].target is not None:
        target = single[1].target

    return AgenticHILConfig(
        config_path=resolved_config_path,
        work_dir=str(workspace),
        workspace_root=str(workspace),
        state_root=str(state_root),
        target=target,
        debuggers=debuggers,
        debug=debug_interface_config(debug_raw),
        artifacts=artifacts_config(artifacts_raw),
        com_ports=document.com_ports,
        can_buses=document.can_buses,
        validation=validation_config(validation_raw),
        reports=reports_config(reports_raw),
        logs=logs_config(logs_raw),
        recovery=recovery_config(recovery_raw),
        debugger_id=single[0],
        debugger=single[1],
        config_version=document.config_version,
        permissions=project_permissions(permissions_raw),
        config_digest=digest,
        loaded_at=utc_now(),
    )


def bind_debugger(config: AgenticHILConfig, debugger_id: str) -> AgenticHILConfig:
    """Return the config bound to one named probe.

    Everything downstream — the backend, the coordination resource, the audit
    report — acts on a single probe. Resolving the name here once means a wrong
    or unknown name fails before any hardware is touched."""
    debugger = config.debuggers.get(debugger_id)
    if debugger is None:
        raise ConfigError(
            "config_invalid",
            "Unknown debugger name.",
            {"field": "debugger_id", "value": debugger_id, "configured_debuggers": sorted(config.debuggers)},
        )
    # A probe's own target overrides the project target for everything that
    # runs on it. Applying it here rather than at one call site means every
    # consumer of config.target sees the board it is actually driving.
    return replace(config, debugger_id=debugger_id, debugger=debugger, target=debugger.target or config.target)


def load_authoritative_config(expected_workspace: str | Path | None = None) -> AgenticHILConfig:
    expected = Path(expected_workspace or Path.cwd()).resolve()
    config_path = os.environ.get(CONFIG_ENV)
    if config_path:
        requested = Path(config_path).expanduser()
        if not requested.is_absolute():
            raise ConfigError(
                "config_invalid",
                f"{CONFIG_ENV} must contain an absolute path.",
                {"path": config_path, "environment_variable": CONFIG_ENV},
            )
    else:
        requested = project_config_path(expected)
    requested = absolute_without_symlinks(requested)
    resolved = requested.resolve()
    if not resolved.is_file():
        raise ConfigError("config_file_not_found", "Agentic HIL configuration file could not be found.", {"path": str(resolved)})
    if os.lstat(resolved).st_nlink != 1:
        raise ConfigError(
            "config_invalid",
            "The authoritative config must be a single-link regular file.",
            {"path": str(resolved)},
        )
    config = load_config(str(requested))
    workspace = Path(config.work_dir)
    if is_path_within_frozen(requested, workspace) or is_path_within(resolved, workspace):
        raise ConfigError(
            "config_invalid",
            "The authoritative config must be stored outside the workspace.",
            {"path": str(resolved), "workspace_root": config.workspace_root},
        )
    if workspace != expected:
        raise ConfigError(
            "config_invalid",
            "The authoritative config is bound to a different workspace.",
            {"path": str(resolved), "workspace_root": config.workspace_root, "expected_workspace": str(expected)},
        )
    if not config_path and resolved != project_config_path(workspace):
        raise ConfigError(
            "config_invalid",
            "The automatically discovered config is not canonical for this workspace.",
            {"path": str(resolved), "expected_path": str(project_config_path(workspace)), "workspace_root": config.workspace_root},
        )
    return validate_pinned_probe_ownership(pin_configured_paths(pin_configured_executables(config)))


def validate_pinned_probe_ownership(config: AgenticHILConfig) -> AgenticHILConfig:
    # Re-run the probe rules on the pinned config. Pinning rewrites executables
    # and script paths, not probe_id or resource_id, so today this cannot find
    # anything validate_debuggers did not already reject at parse time — it is
    # here so that a future pinning step which does touch those fields cannot
    # quietly hand two names one probe.
    validate_debuggers(config.debuggers, after_pinning=True)
    return config


def probe_selector_key(debugger: DebuggerConfig) -> str | None:
    """The configured probe selector as the backend will actually read it.

    pyOCD strips an optional ``<type>:`` prefix from ``--uid`` before matching,
    so ``stlink:0669FF`` and ``0669FF`` are one selector wearing two spellings.
    Comparison is case-insensitive because pyOCD lowercases both sides."""
    if debugger.probe_id is None:
        return None
    selector = debugger.probe_id
    if debugger.type == "pyocd" and ":" in selector:
        selector = selector.split(":", 1)[1]
    return selector.lower()


def validate_resource_ids(
    debuggers: dict[str, DebuggerConfig],
    com_ports: dict[str, ComPortConfig],
    can_buses: dict[str, CanBusConfig],
) -> None:
    """Entries may share a resource_id; they may not spell one two ways.

    Sharing is the feature. An identical ``resource_id`` on a debugger and on a
    COM port is how an operator says an ST-Link and its virtual COM port are one
    unit, and those entries collapse to one lock exactly as intended.

    Values that differ only in case are the opposite situation and cannot be
    resolved from the text: either it is a typo for one unit, or the operator
    is distinguishing two units by case alone. A lock key folds an opaque
    hardware id on every platform (``types.fold_hardware_id``), so the second
    reading would quietly become the first — two boards sharing one lock,
    serialised against each other with nothing anywhere saying why. Refuse the
    pair and let the operator say which they meant: identical if it is one unit,
    distinct by more than case if it is two.

    Pinning cannot introduce such a pair — it rewrites executables and script
    paths, never resource_id — so unlike validate_debuggers this runs once."""
    first_spelling: dict[str, tuple[str, str]] = {}
    entries: list[tuple[str, str | None]] = [(f"debuggers.{name}", entry.resource_id) for name, entry in debuggers.items()]
    entries += [(f"com_ports.{name}", entry.resource_id) for name, entry in com_ports.items()]
    entries += [(f"can_buses.{name}", entry.resource_id) for name, entry in can_buses.items()]
    for field, resource_id in entries:
        if not resource_id:
            continue
        folded = fold_hardware_id(resource_id)
        seen = first_spelling.get(folded)
        if seen is None:
            first_spelling[folded] = (field, resource_id)
        elif seen[1] != resource_id:
            raise ConfigError(
                "config_invalid",
                "Two entries spell one resource_id in two cases. A resource_id names hardware, so its lock key folds case on every platform and these two would share one lock: make them identical if they are one physical unit, or distinct by more than case if they are two.",
                {"field": f"{field}.resource_id", "resource_id": resource_id, "other_entry": seen[0], "other_resource_id": seen[1]},
            )


def validate_debuggers(debuggers: dict[str, DebuggerConfig], *, after_pinning: bool = False) -> None:
    """Each configured debugger must name a distinct physical probe.

    Two names pointing at one probe is how a plan that says "flash board_b"
    silently flashes board_a. Names are the only routing the operator has, so a
    collision fails closed instead of picking a board for them.

    This runs at config load and therefore may not touch hardware: `doctor`,
    `mcp-stdio` and every test load a config with no probe attached. It rejects
    what is decidable from the text alone. Whether a selector actually resolves
    to exactly one connected probe is enforced at the hardware boundary, where
    enumeration is possible — see PyOCDBackend._resolve_probe_selector."""
    # probe_id decides the hardware, so it is checked on its own. The identity
    # pass below cannot stand in for this: debugger_resource_identity() prefers
    # resource_id and never looks at probe_id in that branch, so two entries
    # naming one probe serial under different lease aliases would both pass.
    probe_owner: dict[str, str] = {}
    for name, debugger in debuggers.items():
        probe_key = probe_selector_key(debugger)
        if probe_key is None:
            continue
        if probe_key in probe_owner:
            raise ConfigError(
                "config_invalid",
                "Two configured debuggers name the same probe_id, so both drive one physical probe; a distinct resource_id only renames the lease and cannot make them different boards.",
                {"field": f"debuggers.{name}.probe_id", "probe_id": debugger.probe_id, "other_debugger": probe_owner[probe_key]},
            )
        # pyOCD matches --uid as a case-insensitive SUBSTRING of a probe's
        # unique ID, so one selector containing another is not two probes: the
        # shorter one selects whatever the longer one selects, and more.
        for other_key, other_name in probe_owner.items():
            if debugger.type != "pyocd" and debuggers[other_name].type != "pyocd":
                continue
            if probe_key in other_key or other_key in probe_key:
                raise ConfigError(
                    "config_invalid",
                    "One configured probe_id contains the other, and pyOCD matches --uid as a case-insensitive substring of a probe's unique ID, so the shorter selector also selects the board the longer one names. Give each debugger the full unique ID of its own probe.",
                    {"field": f"debuggers.{name}.probe_id", "probe_id": debugger.probe_id, "other_debugger": other_name},
                )
        probe_owner[probe_key] = name
    resource_owner: dict[str, str] = {}
    for name, debugger in debuggers.items():
        identity = debugger_resource_identity(debugger)
        if identity in resource_owner:
            stage = " after executable pinning" if after_pinning else ""
            raise ConfigError(
                "config_invalid",
                f"Two configured debuggers resolve to the same coordination resource{stage}; give each debugger the probe_id of its own probe so boards cannot silently share a probe.",
                {"field": f"debuggers.{name}", "resource": identity, "other_debugger": resource_owner[identity]},
            )
        resource_owner[identity] = name


def project_config_directory() -> Path:
    if os.name == "nt":
        config_root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (config_root / "agentic-hil" / "projects").resolve()


def project_config_path(workspace: str | Path) -> Path:
    resolved = Path(workspace).resolve()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name).strip(".-") or "workspace"
    identity = os.path.normcase(str(resolved))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return project_config_directory() / f"{safe_name}-{digest}" / "config.yaml"


def user_state_root() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        root = Path(value) if value is not None else Path.home() / "AppData" / "Local"
    else:
        value = os.environ.get("XDG_STATE_HOME")
        root = Path(value) if value is not None else Path.home() / ".local" / "state"
    root = root.expanduser()
    if not root.is_absolute():
        raise ConfigError("config_invalid", "Agentic HIL state root environment path must be absolute.", {"path": str(root)})
    return safe_directory(root / "agentic-hil")


def ensure_safe_state_root() -> list[str]:
    """Create the default state directory, or fail here rather than later.

    ``init`` runs this before it writes anything, so a state root that cannot be
    created — a component that is a file, a symlinked chain, a device that is not
    there — is reported while nothing has been changed, instead of at the first
    lease of the first run. It changes no permissions on anything that already
    exists, and returns the list of changes it made for the caller to report:
    always empty, kept as a list because the caller publishes the field."""
    user_state_root()
    return []


def tighten_owned_writable_ancestors(target: str | Path) -> list[str]:
    """Remove group/other write from the *current user's own* directories (and a
    final regular file such as a launcher) along ``target``'s chain, so the
    fail-closed executable trust check accepts a umask-002 / private-group home
    without the operator hand-fixing permissions.

    Only components owned by the current user are ever changed; the walk stops at
    the first foreign-owned or symlinked ancestor (e.g. ``/home``, ``/``), so it
    never touches shared or system directories. Sticky world-writable dirs (e.g.
    ``/tmp``) are left as-is. Returns a list of the changes made. POSIX only; on
    Windows the ACL model differs and this is a no-op."""
    if os.name == "nt":
        return []
    actions: list[str] = []
    path = Path(os.path.abspath(os.path.expanduser(str(target))))
    euid = os.geteuid()
    existing = path
    while not os.path.lexists(existing) and existing != existing.parent:
        existing = existing.parent
    for component in (existing, *existing.parents):
        try:
            info = os.lstat(component)
        except OSError:
            break
        if stat.S_ISLNK(info.st_mode):
            break  # never chmod through a symlinked component; the validator rejects it
        if info.st_uid != euid:
            break  # never touch directories we do not own (e.g. /home, /)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and mode & stat.S_ISVTX):
            with suppress(OSError):
                os.chmod(component, mode & ~0o022)
                actions.append(f"removed group/other write on {component}")
    return actions


def project_state_directory(config: AgenticHILConfig) -> Path:
    identity = "\0".join(
        [
            os.path.normcase(str(Path(config.config_path).resolve())),
            os.path.normcase(str(Path(config.work_dir).resolve())),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return derived_state_directory(config.state_root, "projects", digest)


def validated_state_root_paths(value: str, workspace: Path, config_path: str) -> Path:
    """What the two configured roots say about each other, from the text alone.

    Split from ``validated_state_root`` so ``validate_config_document`` can run it
    without creating a directory — the answer is the same on every host, which is
    what makes it safe to run from a document-only check."""
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        raise ConfigError("config_invalid", "state_root must be an absolute path.", {"path": config_path, "field": "state_root", "value": value})
    lexical = absolute_without_symlinks(requested)
    if is_path_within_frozen(lexical, workspace) or is_path_within_frozen(workspace, lexical):
        raise ConfigError("config_invalid", "state_root and workspace_root must not overlap.", {"path": config_path, "field": "state_root", "state_root": str(lexical), "workspace_root": str(workspace)})
    return lexical


def validated_state_root(value: str, workspace: Path, config_path: str) -> Path:
    """The configured state_root, created and openable.

    Reachability and writability, which is function rather than trust: a server
    that cannot create this directory fails later, at the first lease or report,
    somewhere with much less context than here. Who else on the machine could
    write it is not asked — the operator's own processes always can, so an answer
    to that question never bound anything (hardci-hq#95)."""
    lexical = validated_state_root_paths(value, workspace, config_path)
    return safe_directory(lexical)


def derived_state_directory(state_root: str | Path, *parts: str) -> Path:
    """Create/reopen a directory derived from the already-validated state_root.

    Every component is opened without following a link, so a symlinked component
    is refused rather than traversed; that is the same rule ``safe_directory``
    applies to any configured directory and it is about what the path *is*, not
    about who else holds rights on it."""
    base = absolute_without_symlinks(Path(state_root))
    return safe_directory(base.joinpath(*parts))


def pin_configured_paths(config: AgenticHILConfig) -> AgenticHILConfig:
    allowed_roots = [configured_workspace_path(config, root, f"artifacts.allowed_roots[{index}]") for index, root in enumerate(config.artifacts.allowed_roots)]
    upload_directory = configured_workspace_path(config, config.artifacts.upload_directory, "artifacts.upload_directory")
    reports_directory = configured_workspace_path(config, config.reports.directory, "reports.directory")
    logs_directory = configured_workspace_path(config, config.logs.directory, "logs.directory")
    return replace(
        config,
        artifacts=replace(config.artifacts, allowed_roots=allowed_roots, upload_directory=upload_directory),
        reports=replace(config.reports, directory=reports_directory),
        logs=replace(config.logs, directory=logs_directory),
    )


def configured_workspace_path(config: AgenticHILConfig, value: str, field: str) -> str:
    workspace = Path(config.work_dir)
    requested = Path(value)
    lexical = absolute_without_symlinks(requested if requested.is_absolute() else workspace / requested)
    resolved = lexical.resolve()
    if not is_path_within_frozen(lexical, workspace) or not is_path_within(resolved, workspace):
        raise ConfigError(
            "config_invalid",
            "Configured artifact and output paths must remain inside the workspace.",
            {"field": field, "path": str(lexical), "workspace_root": config.workspace_root},
        )
    return str(lexical)


def pin_one_debugger(config: AgenticHILConfig, debugger: DebuggerConfig, field_prefix: str, required: bool) -> DebuggerConfig:
    candidates = {
        "openocd": ["openocd"],
        "stlink": ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"],
        "pyocd": ["pyocd"],
    }[debugger.type]
    configured = debugger.executable
    if configured is None and debugger.type == "stlink":
        from agentic_hil.backends.common import find_stm32_programmer_cli

        configured = find_stm32_programmer_cli()
    pinned = replace(
        debugger,
        executable=configured_executable(config, configured, f"{field_prefix}.executable", candidates=candidates, required=required),
    )
    if required and pinned.type == "openocd":
        pinned = replace(
            pinned,
            interface_cfg=configured_external_file(config, pinned.interface_cfg, f"{field_prefix}.interface_cfg"),
            target_cfg=configured_external_file(config, pinned.target_cfg, f"{field_prefix}.target_cfg"),
        )
    return pinned


def pin_configured_executables(config: AgenticHILConfig) -> AgenticHILConfig:
    gdb_executable = configured_executable(
        config,
        config.debug.gdb_executable,
        "debug.gdb_executable",
        candidates=GDB_AUTODETECT_CANDIDATES,
    )
    can_buses = {
        name: replace(
            bus,
            executable=configured_executable(
                config,
                bus.executable,
                f"can_buses.{name}.executable",
                workspace_relative=True,
                required=bus.adapter == "process",
            ),
        )
        for name, bus in config.can_buses.items()
    }
    # Every named debugger MUST be pinned and validated: a named entry could
    # otherwise point a backend at an unvalidated executable. Whether failing to
    # resolve one is fatal is `debugger_drives_hardware`: an entry that grants
    # nothing, and the starter entry `init` writes before a board is attached,
    # do not force an operator to install a toolchain they may never drive.
    debuggers = {name: pin_one_debugger(config, named, f"debuggers.{name}", debugger_drives_hardware(named)) for name, named in config.debuggers.items()}
    return replace(
        config,
        debuggers=debuggers,
        # Re-point the binding at the pinned entry; leaving the pre-pin object
        # bound would run the backend on the unvalidated executable.
        debugger=None if config.debugger_id is None else debuggers[config.debugger_id],
        debug=replace(config.debug, gdb_executable=gdb_executable),
        can_buses=can_buses,
    )


def configured_executable(
    config: AgenticHILConfig,
    executable: str | None,
    field: str,
    *,
    workspace_relative: bool = False,
    candidates: list[str] | None = None,
    required: bool = False,
) -> str | None:
    if executable is None:
        for candidate in candidates or []:
            found = shutil.which(candidate)
            if found is not None:
                executable = found
                break
        if executable is None:
            if required:
                raise ConfigError(
                    "config_invalid",
                    "The config enables hardware access but its executable could not be resolved at startup.",
                    {"field": field},
                )
            return disabled_executable_path(config, field)
    requested = Path(executable)
    has_path_separator = "/" in executable or "\\" in executable
    if requested.is_absolute() or has_path_separator or workspace_relative:
        resolved = Path(resolve_work_path(config, executable))
    else:
        found = shutil.which(executable)
        if found is None:
            raise ConfigError(
                "config_invalid",
                "Configured executable could not be resolved at startup.",
                {"field": field, "value": executable},
            )
        resolved = Path(found).resolve()
    if is_path_within(resolved, Path(config.work_dir)):
        raise ConfigError(
            "config_invalid",
            "Configured executables must be stored outside the workspace.",
            {"field": field, "path": str(resolved), "workspace_root": config.workspace_root},
        )
    if not resolved.is_file() or os.lstat(resolved).st_nlink != 1:
        raise ConfigError(
            "config_invalid",
            "Configured executable must be an existing single-link regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


# What pinning writes in place of an executable that resolved to nothing and did
# not have to. The name is the record: a path with this prefix is not a toolchain
# somebody configured, and `executable_is_disabled` is how a later reader — a
# `doctor` run, `debugger_is_placeholder` — knows the difference on a config that
# has already been pinned.
DISABLED_EXECUTABLE_PREFIX = ".agentic-hil-disabled-"


def disabled_executable_path(config: AgenticHILConfig, field: str) -> str:
    safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", field)
    return str(Path(config.config_path).parent / f"{DISABLED_EXECUTABLE_PREFIX}{safe_field}")


def executable_is_disabled(executable: str) -> bool:
    """Whether this is the placeholder pinning writes rather than a real toolchain."""
    return Path(executable).name.startswith(DISABLED_EXECUTABLE_PREFIX)


def configured_external_file(config: AgenticHILConfig, value: str, field: str) -> str:
    requested = Path(value)
    if not requested.is_absolute():
        raise ConfigError(
            "config_invalid",
            "Configured debugger files must use absolute paths.",
            {"field": field, "value": value},
        )
    resolved = requested.resolve()
    if is_path_within(resolved, Path(config.work_dir)):
        raise ConfigError(
            "config_invalid",
            "Configured debugger files must be stored outside the workspace.",
            {"field": field, "path": str(resolved), "workspace_root": config.workspace_root},
        )
    if not resolved.is_file() or os.lstat(resolved).st_nlink != 1:
        raise ConfigError(
            "config_invalid",
            "Configured debugger file must be an existing single-link regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_path_within_frozen(path: Path, root: Path) -> bool:
    try:
        absolute_without_symlinks(path).relative_to(absolute_without_symlinks(root))
        return True
    except ValueError:
        return False


def absolute_without_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(path))


def configured_work_path(config: AgenticHILConfig, requested_path: str) -> Path:
    requested = Path(requested_path)
    return absolute_without_symlinks(requested if requested.is_absolute() else Path(config.work_dir) / requested)


def safe_configured_directory(config: AgenticHILConfig, requested_path: str, field: str) -> str:
    directory = configured_work_path(config, requested_path)
    if not is_path_within_frozen(directory, Path(config.work_dir)):
        raise ConfigError(
            "unsafe_configured_path",
            "Configured output directory contains a symlink or leaves the workspace.",
            {"field": field, "path": str(directory)},
        )
    if os.name != "nt":
        descriptor = _open_directory_fd(directory, create=True)
        os.close(descriptor)
    else:
        handles = _windows_hold_directory_chain(directory, create=True)
        _close_windows_handles(handles)
    return str(directory)


def safe_directory(directory: str | Path) -> Path:
    path = absolute_without_symlinks(Path(directory))
    if os.name != "nt":
        descriptor = _open_directory_fd(path, create=True)
        os.close(descriptor)
    else:
        handles = _windows_hold_directory_chain(path, create=True)
        _close_windows_handles(handles)
    return path


def secure_optional_read_bytes(file_path: str | Path) -> bytes | None:
    """Read a user file as bytes, or return None when it is absent.

    The directory chain, the file type and the link count are validated while the
    opened descriptor/handle pins the object, so the bytes returned are the bytes
    of the object that was named and not of one substituted mid-read.

    Bytes rather than text, because a caller that needs both a digest of a file
    and the document inside it must take one snapshot and answer out of it; the
    digest is over the exact bytes and a second read can straddle an edit.
    """
    path = absolute_without_symlinks(Path(file_path))
    safe_directory(path.parent)
    try:
        with safe_open_binary(path) as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def secure_optional_read_text(file_path: str | Path, *, encoding: str = "utf-8") -> str | None:
    """The same read, decoded. Raises ``UnicodeDecodeError`` on bytes that are not
    text in ``encoding``; callers that answer for a file somebody else wrote have
    to normalize that rather than let it out as an internal error."""
    raw = secure_optional_read_bytes(file_path)
    return None if raw is None else raw.decode(encoding)


def secure_atomic_write_text(file_path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace one user file with owner-only content."""
    path = absolute_without_symlinks(Path(file_path))
    safe_directory(path.parent)
    # Refuse an existing alias before atomic replacement. Merely replacing a
    # hardlink would hide that a caller selected the wrong object.
    secure_optional_read_text(path, encoding=encoding)
    atomic_write_text(path, text, encoding=encoding)
    written = secure_optional_read_text(path, encoding=encoding)
    if written is None:
        raise ConfigError("unsafe_configured_path", "User configuration file disappeared after replacement.", {"field": "user_config", "path": str(path)})


def secure_remove_file(file_path: str | Path) -> None:
    """Remove a single-link regular file, primarily for transaction rollback."""
    path = absolute_without_symlinks(Path(file_path))
    safe_directory(path.parent)
    if secure_optional_read_text(path) is None:
        return
    if os.name == "nt":
        handles = _windows_hold_directory_chain(path.parent)
        try:
            safe_file_path(path)
            path.unlink()
        finally:
            _close_windows_handles(handles)
        return
    parent_descriptor = _open_directory_fd(path.parent)
    try:
        opened = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ConfigError("unsafe_configured_path", "Rollback target must be a single-link regular file.", {"field": "user_config", "path": str(path)})
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def user_file_lock_path(file_path: str | Path) -> Path:
    """The sidecar a locked transaction leaves beside its file.

    It outlives the transaction, so anything that cleans up a directory this
    tool wrote has to know the name.
    """
    path = Path(file_path)
    return path.with_name(f".{path.name}.agentic-hil.lock")


@contextmanager
def secure_user_file_lock(file_path: str | Path) -> Iterator[None]:
    """Cross-process sidecar lock for a trusted user-file transaction."""
    path = absolute_without_symlinks(Path(file_path))
    safe_directory(path.parent)
    lock_path = user_file_lock_path(path)
    secure_optional_read_text(lock_path)
    with safe_file_lock(lock_path):
        # Validate the object created/opened by safe_file_lock before trusting the
        # lock: it must still be the single-link regular file that was opened.
        # Windows opens this sidecar without read sharing, so reopening it there
        # fails even though the lock itself is valid — and ``safe_file_lock`` has
        # already validated the handle it holds, so nothing is left to re-check.
        if os.name != "nt":
            secure_optional_read_text(lock_path)
        yield


# The skeleton every generated configuration starts from — `agentic-hil init` on
# the CLI and the one-time agent provisioning path over MCP write the same file.
# It lives here rather than beside either caller because a second skeleton is a
# second set of defaults, and defaults are the thing this file exists to keep
# honest.
#
# Every permission in it is true (hardci-hq#96). The previous skeleton granted
# nothing and left opening it as hand work on YAML, which cost this project's
# owner a working day; the property that survives is not the closed start but the
# direction of travel: an agent may write `false` into a permission and never
# `true`, so the bench an operator receives is workable and narrowing it is what
# an agent does on request. `project_config_set` enforces that from the document
# rather than from the request.
DEFAULT_CONFIG_TEMPLATE = """# Version 2: reading a device needs no permission. Every device a test plan
# names is locked machine-wide for the whole run and a device the plan does not
# name is refused, so an observation from outside cannot reach into a run, and a
# reader while no run holds the board disturbs nobody. Writing and
# state-changing operations are unchanged. A config
# written without this key is read under version 1, where reading still needs
# allow_probe / allow_read.
version: 2

# What may be done to this file itself. All three start true, so an agent can
# describe the bench, regenerate it from hardware discovery, and narrow any
# permission here on your say-so — without you opening YAML first.
#
# It can only ever narrow. Nothing an agent calls writes `true` into a
# permission, so every one of these can go from true to false and none of them
# back. allow_config_permissions_write is therefore the last one an agent can
# close: after that it cannot change any permission again, and
# `agentic-hil init --force` is what reopens the file.
permissions:
  allow_config_write: true
  allow_config_description_write: true
  allow_config_permissions_write: true

target:
  name: "example-target"
  controller: "unknown-controller"

# Every debug probe is a named entry, and each carries its own permissions,
# scoped to that probe alone — narrowing one board does not narrow a second that
# shares this file. Test-reactor plan steps address a probe by that name. The MCP
# tools drive one probe: with exactly one entry it is bound automatically, and
# with several they refuse rather than pick a board, so multi-board work runs
# through `agentic-hil test-reactor`.
debuggers:
  dut:
    type: "openocd"
    executable: null
    probe_id: null
    target_type: null
    interface_cfg: "interface/stlink.cfg"
    target_cfg: "target/stm32f4x.cfg"
    timeout_s: 60
    permissions:
      allow_flash: true
      allow_reset: true
      allow_raw_debugger_commands: true
      # A mass erase cannot be taken back: whatever was on the board is gone and
      # no later setting returns it. It starts true anyway, because the person
      # running these benches owns them. Set it false — or ask an agent to — on
      # any bench where a foreign board can end up in the socket.
      allow_mass_erase: true

debug:
  gdb_executable: null
  allowed_symbols: []
  allow_all_symbols: true
  max_dump_size_bytes: 1048576

artifacts:
  # "." is the whole workspace, recursively: firmware may be flashed from any
  # directory under workspace_root. Build layouts differ per toolchain —
  # cmake-build-debug, .pio/build/<env>, out, Debug, zephyr/build — and
  # enumerating them here restricts a project against its own operator, not
  # against an attacker: containment in workspace_root, the extension list and
  # the format check are what actually refuse a foreign artifact, and none of
  # them depends on this list. Name directories instead of "." to narrow it.
  # A configuration that omits this key keeps the older ["build"].
  allowed_roots:
    - "."
  upload_directory: ".agentic-hil/artifacts"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"
  max_upload_size_mb: 64
  allow_upload: true

com_ports: {}

can_buses: {}

validation:
  require_existing_file: true
  require_allowed_root: true
  require_allowed_extension: true
  compute_sha256: true
  inspect_known_formats: true

reports:
  directory: ".agentic-hil/reports"

logs:
  directory: ".agentic-hil/logs"

# How far this bench lets the owning process clear its own hardware
# quarantines. "off" always defers to the operator. "readonly" may reap
# leftover debugger processes and re-read the probe, which touches nothing
# physical. "reset_halt" may additionally drive a reset-into-halt, which is
# what settles an unconfirmed flash or reset without a person — set
# "readonly" instead if anything on this bench reacts to a target reset.
recovery:
  auto_recover: "reset_halt"
  max_attempts: 3
"""


# ---------------------------------------------------------------------------
# One-time agent provisioning of a project configuration.
#
# An agent that finds no configuration may generate one, and the file it
# generates is workable: every permission true (hardci-hq#96). The ratchet did
# not disappear with the closed start, it turned around. It used to be "an agent
# enables itself up to observation and never up to modification"; it is now "an
# agent can only ever reduce its own authority", because nothing on this surface
# writes `true` into a permission. Both are the same statement about direction,
# and only the second one hands an operator a bench they can use.
#
# The ratchet lives in the configuration itself, and there is no second state
# store anywhere. Two states, both readable from the one file a human inspects:
# no configuration for the workspace, and the agent may generate one; a
# configuration exists, and its own permissions.allow_config_write decides
# whether the agent may write it.
#
# Deleting the configuration out of band therefore lets an agent generate a fresh
# one. Under the closed default that was a downgrade; under this one it restores
# what a generation grants, and the honest thing to say about it is that an agent
# with a shell was never held back by the file's contents — the deny rules
# `setup` writes into the agent host are what keep its own file tools off this
# path, and they are untouched. What the generation still cannot do is produce
# content of its own choosing: it is this fixed skeleton filled from hardware
# discovery, so what comes back is what an operator would have got from
# `agentic-hil init`, minus whatever they had narrowed. Nothing on the MCP
# surface deletes or moves a configuration.
#
# An external record was tried and removed. It could not be a place the same OS
# user cannot write — that needs a second principal, the compromise 0018 refused
# — so against an agent with a shell it was a second thing to delete rather than
# a boundary, while it moved part of the effective policy out of the file an
# operator reads and blocked an operator who deliberately deletes a
# configuration to start over.
# Every permission a generated configuration decides, per section. The write
# class of decision 0018, plus the project-scoped grants below. A generation
# writes every one of them true; the list has to be complete in either direction,
# because a flag missing from it is one the skeleton alone decides.
GENERATED_WRITE_PERMISSIONS = {
    "debuggers": ("allow_flash", "allow_reset", "allow_raw_debugger_commands", "allow_mass_erase"),
    "com_ports": ("allow_write",),
    "can_buses": ("allow_write",),
}
# The project-scoped grants, all three of them. A generated configuration writes
# every one true and a regenerated one carries every one over: whichever list is
# short is the one that hands out a grant nobody set or drops one somebody did.
GENERATED_PROJECT_PERMISSIONS = ("allow_config_write", "allow_config_description_write", "allow_config_permissions_write")
# The two grants the schema puts directly on a fixed section rather than inside a
# `permissions` block. A generation writes both true, so they belong in every
# list that claims to say what a generated configuration grants — and in the key
# model, or an operator could take them back only by opening the file.
GENERATED_SECTION_PERMISSIONS = {"debug": ("allow_all_symbols",), "artifacts": ("allow_upload",)}


def permission_summary(config: AgenticHILConfig) -> JsonObject:
    """Every grant a loaded configuration holds, by name.

    Read off the parsed policy rather than off a document, which is what makes
    it the answer when there is no document left to read: a server whose
    configuration was removed underneath it still enforces what it loaded, and
    this is the only shape in which an operator can be shown what that is.

    Complete, including the two section-level grants — a summary that leaves a
    grant out is one an operator reads as "not granted"."""
    return {
        **{name: bool(getattr(config.permissions, name, False)) for name in GENERATED_PROJECT_PERMISSIONS},
        "debuggers": {name: {flag: bool(getattr(entry.permissions, flag)) for flag in GENERATED_WRITE_PERMISSIONS["debuggers"]} for name, entry in config.debuggers.items()},
        "com_ports": {name: {"allow_write": entry.permissions.allow_write} for name, entry in config.com_ports.items()},
        "can_buses": {name: {"allow_write": entry.permissions.allow_write} for name, entry in config.can_buses.items()},
        "debug": {"allow_all_symbols": bool(config.debug.allow_all_symbols)},
        "artifacts": {"allow_upload": bool(config.artifacts.allow_upload)},
    }


def authoritative_config_target(workspace: Path) -> Path:
    """Where this workspace's authoritative configuration belongs.

    The operator's ``AGENTIC_HIL_CONFIG`` wins when it is set, because that is
    the one location a running server actually reads; otherwise the canonical
    per-workspace path. Never inside the workspace, where repository content
    could rewrite policy."""
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        requested = Path(configured).expanduser()
        if not requested.is_absolute():
            raise ConfigError("config_invalid", f"{CONFIG_ENV} must contain an absolute path.", {"path": configured, "environment_variable": CONFIG_ENV})
        target = absolute_without_symlinks(requested)
    else:
        target = project_config_path(workspace)
    if is_path_within_frozen(target, workspace):
        raise ConfigError("config_invalid", "The authoritative config must be stored outside the workspace.", {"path": str(target), "workspace_root": str(workspace)})
    return target


def provisionable_state_root(workspace: Path) -> Path:
    """A ``state_root`` this profile actually accepts, or the actionable refusal.

    The documented default lands under ``%LOCALAPPDATA%``/``$XDG_STATE_HOME``,
    and on a stock Windows 11 profile that inherits AppData's app-capability ACE
    and is rejected (issue #64). A generated configuration that named it would be
    written and then refused on load, so the trusted fallback under
    ``~/.agentic-hil`` — the same location every refusal already recommends — is
    tried next. When neither passes, the caller gets the same
    ``unsafe_configured_path`` refusal, carrying the same remediation, that the
    CLI returns."""
    candidates: list[Path] = []
    failure: ConfigError | None = None
    try:
        candidates.append(user_state_root())
    except ConfigError as error:
        failure = error
    fallback = absolute_without_symlinks(Path(safe_state_root_suggestion()))
    if not any(os.path.normcase(str(item)) == os.path.normcase(str(fallback)) for item in candidates):
        candidates.append(fallback)
    for candidate in candidates:
        if is_path_within_frozen(candidate, workspace) or is_path_within_frozen(workspace, candidate):
            failure = ConfigError(
                "config_invalid",
                "state_root and workspace_root must not overlap.",
                {"field": "state_root", "state_root": str(candidate), "workspace_root": str(workspace)},
            )
            continue
        try:
            return safe_directory(candidate)
        except ConfigError as error:
            failure = error
    raise failure or ConfigError("unsafe_configured_path", "No trusted state_root location is available on this profile.", {"field": "state_root"})


def grant_every_permission(document: JsonObject) -> JsonObject:
    """Set every permission in a generated document to true.

    The generated skeleton already says so; this makes it hold regardless of what
    filled it in, and that is the part worth keeping from the version of this
    function that wrote `false` everywhere: a generation decides the permission
    state *completely* rather than half. Whatever a workspace profile asked for,
    whatever a template edit forgot, the answer here is the same one, and it is
    the same one for `agentic-hil init` and for `project_config_create`.

    Nothing about it hands an agent something it can keep widening. Every
    permission is open the moment the file exists, so there is nothing left to
    gain by calling this again; the direction that still holds is the other one,
    enforced in `configwrite.project_config_set`, where an agent may write
    `false` into a permission and never `true`."""
    for section, flags in GENERATED_WRITE_PERMISSIONS.items():
        entries = document.get(section)
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if isinstance(entry, dict):
                entry["permissions"] = dict.fromkeys(flags, True)
    for section, grants in GENERATED_SECTION_PERMISSIONS.items():
        node = document.get(section)
        if isinstance(node, dict):
            node.update(dict.fromkeys(grants, True))
    document["permissions"] = dict.fromkeys(GENERATED_PROJECT_PERMISSIONS, True)
    return document


def carry_over_permissions(document: JsonObject, existing: AgenticHILConfig) -> list[str]:
    """Give a regenerated document exactly the grants the existing file has.

    Regeneration refreshes what the hardware is, never what it may do. Every
    permission is copied from the configuration on disk, by name, so a narrowing
    somebody made survives a call taken to refresh a probe id and no permission
    the file does not already hold appears; a device the regenerated document
    does not contain is returned to the caller rather than dropped in silence.

    This is the reason regeneration is not a way around the one-way rule. An
    agent that narrowed a permission and then called `project_config_create`
    would get the narrowed file back, not the open skeleton — the skeleton's
    grants reach only a workspace that has no configuration at all."""
    sources: dict[str, dict[str, Any]] = {
        "debuggers": dict(existing.debuggers),
        "com_ports": dict(existing.com_ports),
        "can_buses": dict(existing.can_buses),
    }
    for section, flags in GENERATED_WRITE_PERMISSIONS.items():
        entries = document.get(section)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            previous = sources[section].get(name)
            if not isinstance(entry, dict) or previous is None:
                continue
            granted = previous.permissions
            entry["permissions"] = {flag: bool(getattr(granted, flag, False)) for flag in flags}
    artifacts = document.get("artifacts")
    if isinstance(artifacts, dict):
        artifacts["allow_upload"] = existing.artifacts.allow_upload
        # Where firmware may be flashed from belongs to the operator exactly as a
        # grant does. The skeleton names the whole workspace, so a regeneration
        # that kept the skeleton's value would widen a file that had narrowed it
        # — silently, on a path a person opened for a hardware refresh. Config
        # load pins these to absolute paths; write them back as they were read.
        artifacts["allowed_roots"] = [display_path(existing, root) for root in existing.artifacts.allowed_roots]
        # And which formats may reach a backend, for the same reason and more
        # sharply: with the roots naming the whole workspace, the extension list
        # is one of the checks that still refuse a foreign artifact. A bench
        # narrowed to [".bin"] would come back out of a hardware refresh
        # accepting .elf and .hex again — three format parsers where its owner
        # had permitted one.
        #
        # upload_directory and max_upload_size_mb are deliberately not here.
        # They place a directory and size a payload rather than say what may be
        # flashed, and regeneration re-derives placement from the environment on
        # purpose — state_root above is recomputed the same way. debug carries
        # allowed_symbols and leaves max_dump_size_bytes beside it alone; this
        # is that line, in the section next to it.
        artifacts["allowed_extensions"] = list(existing.artifacts.allowed_extensions)
    debug = document.get("debug")
    if isinstance(debug, dict):
        debug["allow_all_symbols"] = existing.debug.allow_all_symbols
        debug["allowed_symbols"] = list(existing.debug.allowed_symbols)
    document["permissions"] = {name: bool(getattr(existing.permissions, name, False)) for name in GENERATED_PROJECT_PERMISSIONS}
    return sorted(f"{section}.{name}" for section, entries in sources.items() for name in entries if name not in (document.get(section) or {}))


def write_generated_config(target_path: Path, workspace: Path, text: str) -> None:
    """Validate generated configuration text, then make it the authoritative file.

    Validation runs on a temporary file in the target directory first, so a
    document that would not load never replaces one that does."""
    safe_directory(target_path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".agentic-hil-config-validate-", dir=target_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        load_config(str(temporary_path), str(workspace))
    finally:
        secure_remove_file(temporary_path)
    secure_atomic_write_text(target_path, text)


def trusted_persistent_executable(
    executable: str | Path,
    *,
    workspace: str | Path,
    disallowed_roots: list[str | Path] | None = None,
) -> str:
    """Pin a stable executable outside the workspace and cache/temp roots.

    One owner-controlled POSIX launcher symlink is supported for pipx-style
    ``~/.local/bin`` installs. Both the link and its direct target are pinned and
    validated; nested links and changing links fail closed.
    """
    requested = Path(executable).expanduser()
    if not requested.is_absolute():
        raise ConfigError("mcp_command_untrusted", "The MCP server command must resolve to an absolute path.", {"path": str(requested)})
    path = absolute_without_symlinks(requested)
    workspace_path = absolute_without_symlinks(Path(workspace))
    # Both shapes of every root: macOS reports its temporary directory as
    # /var/folders while a path under it resolves to /private/var/folders, and a
    # root that only matches in one shape is a root that can be walked around.
    # Widening a refusal is always safe; narrowing one is not.
    forbidden = [workspace_path]
    for root in [Path(workspace), *(disallowed_roots or [])]:
        expanded = Path(root).expanduser()
        forbidden.append(absolute_without_symlinks(expanded))
        with suppress(OSError):
            forbidden.append(expanded.resolve())

    def reject_forbidden(candidate: Path) -> None:
        shapes = [candidate]
        with suppress(OSError):
            shapes.append(candidate.resolve())
        if any(is_path_within_frozen(shape, root) for shape in shapes for root in forbidden):
            raise ConfigError("mcp_command_untrusted", "The MCP server command must not come from the workspace or a temporary/cache directory.", {"path": str(candidate)})

    reject_forbidden(path)

    if os.name == "nt":
        # The chain is held open while the file is: that blocks a rename or a
        # delete of any component for the duration, which is what stops the
        # command that gets written into an agent's configuration from being a
        # different object than the one that was checked. Who else the ACLs let
        # write it is no longer asked — see hardci-hq#95.
        handles = _windows_hold_directory_chain(path.parent)
        try:
            with safe_open_binary(path):
                pass
        finally:
            _close_windows_handles(handles)
        return str(path)

    def trusted_parent_chain(candidate: Path) -> list[int]:
        descriptors = _hold_posix_directory_chain(candidate.parent)
        try:
            for index, descriptor in enumerate(descriptors):
                opened = os.fstat(descriptor)
                mode = stat.S_IMODE(opened.st_mode)
                final = index == len(descriptors) - 1
                unsafe_write = bool(mode & 0o022) and (final or not bool(mode & stat.S_ISVTX))
                if unsafe_write or (final and opened.st_uid not in {0, os.geteuid()}):
                    # Name the component and its mode: the caller cannot fix a
                    # directory the message does not identify, and one bad
                    # ancestor of a shared prefix condemns every launcher below.
                    parents = list(candidate.parents)
                    position = len(descriptors) - 1 - index
                    ancestor = parents[position] if 0 <= position < len(parents) else candidate.parent
                    raise ConfigError(
                        "mcp_command_untrusted",
                        "The MCP server executable has an untrusted or replaceable parent directory.",
                        {"path": str(candidate), "directory": str(ancestor), "mode": f"{mode:04o}", "uid": opened.st_uid},
                    )
            return descriptors
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    def validate_executable_file(candidate: Path) -> None:
        reject_forbidden(candidate)
        target_descriptors = trusted_parent_chain(candidate)
        try:
            with safe_open_binary(candidate) as handle:
                opened = os.fstat(handle.fileno())
                mode = stat.S_IMODE(opened.st_mode)
                if opened.st_uid not in {0, os.geteuid()} or mode & 0o022 or not mode & 0o111:
                    raise ConfigError("mcp_command_untrusted", "The MCP server executable must be trusted, executable, and not writable by other users.", {"path": str(candidate)})
        finally:
            for descriptor in reversed(target_descriptors):
                os.close(descriptor)

    descriptors = trusted_parent_chain(path)
    try:
        parent_descriptor = descriptors[-1]
        first = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(first.st_mode):
            if first.st_uid not in {0, os.geteuid()} or first.st_nlink != 1:
                raise ConfigError("mcp_command_untrusted", "The MCP launcher symlink must have a trusted owner and one link.", {"path": str(path)})
            link_value = os.readlink(path.name, dir_fd=parent_descriptor)
            current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if not os.path.samestat(first, current):
                raise ConfigError("mcp_command_untrusted", "The MCP launcher symlink changed during validation.", {"path": str(path)})
            direct_target = Path(link_value)
            direct_target = absolute_without_symlinks(direct_target if direct_target.is_absolute() else path.parent / direct_target)
            try:
                resolved_target = direct_target.resolve(strict=True)
            except OSError as error:
                raise ConfigError("mcp_command_untrusted", "The MCP launcher symlink target does not exist.", {"path": str(path)}) from error
            if resolved_target != direct_target:
                raise ConfigError("mcp_command_untrusted", "The MCP launcher symlink target must not contain another symlink.", {"path": str(path), "target": str(direct_target)})
            validate_executable_file(direct_target)
            final = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if not os.path.samestat(first, final):
                raise ConfigError("mcp_command_untrusted", "The MCP launcher symlink changed during validation.", {"path": str(path)})
            return str(path)
        if not stat.S_ISREG(first.st_mode):
            raise ConfigError("mcp_command_untrusted", "The MCP server command must be a regular executable or one trusted launcher symlink.", {"path": str(path)})
        validate_executable_file(path)
        final = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not os.path.samestat(first, final):
            raise ConfigError("mcp_command_untrusted", "The MCP server executable changed during validation.", {"path": str(path)})
        return str(path)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def safe_file_path(file_path: str | Path, workspace: str | Path | None = None) -> Path:
    path = _validated_absolute_file_path(file_path, workspace)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if path.parent.resolve() != path.parent or (existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1)):
        raise ConfigError(
            "unsafe_configured_path",
            "Output file must be a single-link regular file without symlinked parents.",
            {"path": str(path)},
        )
    return path


def _validated_absolute_file_path(file_path: str | Path, workspace: str | Path | None = None) -> Path:
    path = absolute_without_symlinks(Path(file_path))
    if workspace is not None and not is_path_within_frozen(path, Path(workspace)):
        raise ConfigError(
            "unsafe_configured_path",
            # Reads reach this too (artifact validation), so do not say "output".
            "Path leaves the workspace.",
            {"path": str(path)},
        )
    return path


def _open_directory_fd(directory: Path, *, create: bool = False) -> int:
    path = absolute_without_symlinks(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    descriptor = os.open(parts[0], flags)
    walked = Path(parts[0])
    try:
        for part in parts[1:]:
            walked = walked / part
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(next_descriptor)
                if not stat.S_ISDIR(opened.st_mode):
                    raise ConfigError("unsafe_configured_path", "Configured path component is not a directory.", {"path": str(path)})
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except ConfigError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        _raise_unsafe_path_error(error, path, walked)
        raise


def _hold_posix_directory_chain(directory: Path, *, create: bool = False) -> list[int]:
    """Open every POSIX directory component and retain all descriptors."""
    path = absolute_without_symlinks(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(parts[0], flags))
        for part in parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(descriptor)
                raise ConfigError("unsafe_configured_path", "Configured path component is not a directory.", {"path": str(path)})
            descriptors.append(descriptor)
        return descriptors
    except ConfigError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        _raise_unsafe_path_error(error, path)
        raise


def _raise_unsafe_path_error(error: OSError, path: Path, component: Path | None = None) -> None:
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        # Name the component that stopped the walk. A caller told only the full
        # path has to guess which link to replace, and a symlinked agent
        # directory is a common, legitimate-looking setup.
        details = {"path": str(path)}
        if component is not None and component != path:
            details["component"] = str(component)
        raise ConfigError(
            "unsafe_configured_path",
            "Configured path contains a symlink or non-directory component.",
            details,
        ) from error


@contextmanager
def _path_lock(path: Path) -> Iterator[None]:
    key = os.path.normcase(str(path))
    with _PATH_LOCKS_GUARD:
        lock, references = _PATH_LOCKS.get(key, (threading.Lock(), 0))
        _PATH_LOCKS[key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _PATH_LOCKS_GUARD:
            current = _PATH_LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] == 1:
                    _PATH_LOCKS.pop(key, None)
                else:
                    _PATH_LOCKS[key] = (lock, current[1] - 1)


@contextmanager
def safe_file_lock(file_path: str | Path, *, workspace: str | Path | None = None) -> Iterator[None]:
    path = _validated_absolute_file_path(file_path, workspace)
    with _path_lock(path):
        if os.name != "nt":
            parent_descriptor = _open_directory_fd(path.parent)
            descriptor = -1
            try:
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
                _validate_open_file(descriptor, path)
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                os.close(parent_descriptor)
            return

        directory_handles = _windows_hold_directory_chain(path.parent)
        descriptor = -1
        try:
            path = safe_file_path(path, workspace)
            descriptor = _windows_open_regular_file(path, read=True, write=True, create=True)
            opened = _validate_open_file(descriptor, path)
            if not os.path.samestat(opened, os.stat(path, follow_symlinks=False)):
                raise ConfigError("unsafe_configured_path", "Lock file changed while it was being opened.", {"path": str(path)})
            if opened.st_size == 0:
                os.write(descriptor, b"0")
            import msvcrt

            # LK_LOCK gives up after ~10 seconds; keep retrying so the Windows
            # branch blocks like the POSIX flock(LOCK_EX) branch does.
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            _close_windows_handles(directory_handles)


def _windows_hold_directory_chain(directory: Path, *, create: bool = False) -> list[int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    file_read_attributes = 0x80
    file_list_directory = 0x1
    file_share_read_write = 0x1 | 0x2
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    file_attribute_directory = 0x10
    file_attribute_reparse_point = 0x400

    path = absolute_without_symlinks(directory)
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current /= part
        components.append(current)

    handles: list[int] = []
    try:
        for component in components:
            if create and not component.exists():
                component.mkdir(exist_ok=True)
            # FILE_LIST_DIRECTORY participates in sharing checks, so this handle
            # blocks renames of the component while coexisting with other holders;
            # attributes-only handles are excluded from sharing and cannot pin.
            handle = create_file(
                str(component),
                file_list_directory | file_read_attributes,
                file_share_read_write,
                None,
                open_existing,
                file_flag_open_reparse_point | file_flag_backup_semantics,
                None,
            )
            if handle == invalid_handle:
                error_code = ctypes.get_last_error()
                if error_code != 5:
                    raise ctypes.WinError(error_code)
                handle = create_file(
                    str(component),
                    file_read_attributes,
                    file_share_read_write,
                    None,
                    open_existing,
                    file_flag_open_reparse_point | file_flag_backup_semantics,
                    None,
                )
                if handle == invalid_handle:
                    raise ctypes.WinError(ctypes.get_last_error())
            numeric_handle = int(handle)
            handles.append(numeric_handle)
            info = ByHandleFileInformation()
            if not get_info(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.dwFileAttributes & file_attribute_reparse_point or not info.dwFileAttributes & file_attribute_directory:
                raise ConfigError("unsafe_configured_path", "Configured path contains a Windows reparse point.", {"path": str(component)})
        return handles
    except BaseException:
        _close_windows_handles(handles)
        raise


def _close_windows_handles(handles: list[int]) -> None:
    if not handles:
        return
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(handle)


def _windows_open_regular_file(
    path: Path,
    *,
    read: bool = False,
    write: bool = False,
    create: bool = False,
    append: bool = False,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    access = (0x80000000 if read else 0) | (0x40000000 if write else 0)
    handle = create_file(
        str(path),
        access,
        0x1 | 0x2,
        None,
        4 if create else 3,
        0x80 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ByHandleFileInformation()
        if get_file_type(handle) != 0x1 or not get_info(handle, ctypes.byref(information)):
            raise ConfigError("unsafe_configured_path", "Configured file is not a disk file.", {"path": str(path)})
        if information.dwFileAttributes & (0x10 | 0x400) or information.nNumberOfLinks != 1:
            raise ConfigError("unsafe_configured_path", "Configured file must be a single-link regular file without reparse points.", {"path": str(path)})
        descriptor_flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor_flags |= os.O_RDWR if read and write else os.O_WRONLY if write else os.O_RDONLY
        if append:
            descriptor_flags |= os.O_APPEND
        descriptor = msvcrt.open_osfhandle(int(handle), descriptor_flags)
        handle = invalid_handle
        return descriptor
    finally:
        if handle != invalid_handle:
            close_handle(handle)


def _validate_open_file(descriptor: int, path: Path) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ConfigError(
            "unsafe_configured_path",
            "Configured file must be a single-link regular file.",
            {"path": str(path)},
        )
    return opened


@contextmanager
def safe_open_binary(file_path: str | Path, *, workspace: str | Path | None = None) -> Iterator[BinaryIO]:
    path = _validated_absolute_file_path(file_path, workspace)
    if os.name != "nt":
        parent_descriptor = _open_directory_fd(path.parent)
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
            except OSError as error:
                _raise_unsafe_path_error(error, path)
                raise
            opened = _validate_open_file(descriptor, path)
            current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if not os.path.samestat(opened, current):
                raise ConfigError("unsafe_configured_path", "Configured file changed while it was being opened.", {"path": str(path)})
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                yield handle
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_descriptor)
        return

    directory_handles = _windows_hold_directory_chain(path.parent)
    descriptor = -1
    try:
        path = safe_file_path(path, workspace)
        descriptor = _windows_open_regular_file(path, read=True)
        opened = _validate_open_file(descriptor, path)
        current = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(opened, current) or path.parent.resolve() != path.parent:
            raise ConfigError("unsafe_configured_path", "Configured file changed while it was being opened.", {"path": str(path)})
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_windows_handles(directory_handles)


def safe_read_bytes(file_path: str | Path, *, workspace: str | Path | None = None) -> bytes:
    with safe_open_binary(file_path, workspace=workspace) as handle:
        return handle.read()


def safe_read_text(
    file_path: str | Path,
    *,
    encoding: str = "utf-8",
    workspace: str | Path | None = None,
) -> str:
    return safe_read_bytes(file_path, workspace=workspace).decode(encoding)


def atomic_write_bytes(
    file_path: str | Path,
    data: bytes,
    *,
    workspace: str | Path | None = None,
) -> None:
    path = _validated_absolute_file_path(file_path, workspace)
    with _path_lock(path):
        if os.name != "nt":
            parent_descriptor = _open_directory_fd(path.parent)
            temporary_name = f".agentic-hil-write-{secrets.token_hex(16)}"
            descriptor = -1
            try:
                try:
                    current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    raise ConfigError("unsafe_configured_path", "Output file must be a single-link regular file.", {"path": str(path)})
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path.name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                os.close(parent_descriptor)
            return

        directory_handles = _windows_hold_directory_chain(path.parent)
        try:
            path = safe_file_path(path, workspace)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".agentic-hil-write-", dir=path.parent)
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        finally:
            _close_windows_handles(directory_handles)


def atomic_write_text(
    file_path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    workspace: str | Path | None = None,
) -> None:
    atomic_write_bytes(file_path, text.encode(encoding), workspace=workspace)


def safe_append_text(file_path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    path = _validated_absolute_file_path(file_path)
    data = text.encode(encoding)
    with _path_lock(path):
        if os.name != "nt":
            parent_descriptor = _open_directory_fd(path.parent)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                try:
                    descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
                except OSError as error:
                    _raise_unsafe_path_error(error, path)
                    raise
            finally:
                os.close(parent_descriptor)
            try:
                _validate_open_file(descriptor, path)
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return

        directory_handles = _windows_hold_directory_chain(path.parent)
        try:
            path = safe_file_path(path)
            lock_path = path.with_name(f".{path.name}.lock")
            safe_file_path(lock_path)
            lock_descriptor = _windows_open_regular_file(lock_path, read=True, write=True, create=True)
            try:
                lock_stat = _validate_open_file(lock_descriptor, lock_path)
                if not os.path.samestat(lock_stat, os.stat(lock_path, follow_symlinks=False)):
                    raise ConfigError("unsafe_configured_path", "Append lock changed while it was being opened.", {"path": str(lock_path)})
                if os.fstat(lock_descriptor).st_size == 0:
                    os.write(lock_descriptor, b"0")
                os.lseek(lock_descriptor, 0, os.SEEK_SET)
                import msvcrt

                msvcrt.locking(lock_descriptor, msvcrt.LK_LOCK, 1)
                descriptor = _windows_open_regular_file(path, write=True, create=True, append=True)
                try:
                    opened = _validate_open_file(descriptor, path)
                    current = os.stat(path, follow_symlinks=False)
                    if not os.path.samestat(opened, current):
                        raise ConfigError("unsafe_configured_path", "Append target changed while it was being opened.", {"path": str(path)})
                    _write_all(descriptor, data)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                    os.lseek(lock_descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(lock_descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(lock_descriptor)
        finally:
            _close_windows_handles(directory_handles)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("File write made no progress.")
        view = view[written:]


def safe_write_text(
    config: AgenticHILConfig,
    file_path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    atomic_write_text(file_path, text, encoding=encoding, workspace=config.work_dir)


def resolve_work_path(config: AgenticHILConfig, requested_path: str) -> str:
    requested = Path(requested_path)
    candidate = requested if requested.is_absolute() else Path(config.work_dir) / requested
    return str(candidate.resolve())


def display_path(config: AgenticHILConfig, requested_path: str) -> str:
    requested = Path(requested_path)
    if not requested.is_absolute():
        return to_posix(str(requested))
    try:
        return to_posix(str(requested.resolve().relative_to(Path(config.work_dir).resolve())))
    except ValueError:
        return str(requested_path)


def to_posix(value: str) -> str:
    return value.replace("\\", "/")


def raise_config_validation_error(error: Any, config_path: str | None = None) -> None:
    details: JsonObject = {"field": schema_error_field(error)}
    if config_path is not None:
        details["path"] = config_path

    if error.validator == "additionalProperties":
        details["allowed_fields"] = sorted((error.schema.get("properties") or {}).keys())
        raise ConfigError("config_invalid", "Unknown Agentic HIL configuration field.", details) from error
    if error.validator == "enum":
        details["allowed_values"] = error.validator_value
        add_scalar_schema_value(details, error.instance)
        raise ConfigError("config_invalid", f"{details['field']} has an unsupported value.", details) from error
    if error.validator == "type":
        details["expected_type"] = error.validator_value
        add_scalar_schema_value(details, error.instance)
        raise ConfigError("config_invalid", f"{details['field']} has the wrong type.", details) from error

    details["validator"] = error.validator
    details["schema_error"] = "Value does not satisfy the Agentic HIL configuration schema."
    add_scalar_schema_value(details, error.instance)
    raise ConfigError("config_invalid", "Agentic HIL configuration failed schema validation.", details) from error


def add_scalar_schema_value(details: JsonObject, value: object) -> None:
    if isinstance(value, str):
        details["value"] = value[:128]
    elif isinstance(value, float) and not math.isfinite(value):
        details["value"] = "non-finite"
    elif isinstance(value, (int, float, bool)) or value is None:
        details["value"] = value


def reject_nonfinite_numbers(value: object, error_type: str, path: str | None = None, parts: list[str] | None = None) -> None:
    field_parts = parts or []
    if isinstance(value, float) and not math.isfinite(value):
        details: JsonObject = {"field": format_field_path(field_parts), "value": "non-finite"}
        if path is not None:
            details["path"] = path
        raise ConfigError(error_type, "Configuration values must be finite numbers.", details)
    if isinstance(value, dict):
        for key, child in value.items():
            reject_nonfinite_numbers(child, error_type, path, [*field_parts, str(key)])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_nonfinite_numbers(child, error_type, path, [*field_parts, str(index)])


def schema_error_field(error: Any) -> str:
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "additionalProperties":
        match = re.search(r"'([^']+)' was unexpected", error.message)
        if match:
            parts.append(match.group(1))
    return format_field_path(parts)


def format_field_path(parts: list[str]) -> str:
    result = ""
    for part in parts:
        if part.isdigit():
            result = f"{result}[{part}]" if result else f"[{part}]"
        else:
            result = f"{result}.{part}" if result else part
    return result or "$"


def target_config(raw: JsonObject) -> TargetConfig:
    return TargetConfig(name=str(raw.get("name", "unknown-target")), controller=str(raw.get("controller", "unknown-controller")))


def debugger_resource_identity(debugger: DebuggerConfig) -> str:
    # Mirror devices.DebuggerDevice.lock_key so config validation rejects exactly
    # the collisions the coordinator would (and OpenOCD would silently share).
    # It stays a mirror rather than a call because devices.py sits above this
    # module in the import graph; tests/test_devices.py asserts the two agree,
    # case included, so they cannot drift.
    if debugger.resource_id:
        return f"physical:{fold_hardware_id(debugger.resource_id)}"
    if debugger.probe_id:
        return f"probe:{fold_hardware_id(debugger.probe_id)}"
    if debugger.executable:
        return f"probe:{fold_device_path(debugger.executable)}"
    return f"probe:{fold_hardware_id(debugger.type)}"


def named_debugger_config(name: str, value: Any, default_target: TargetConfig) -> DebuggerConfig:
    field = f"debuggers.{name}"
    raw = mapping(value, field)
    debugger_type = str(raw.get("type", "openocd"))
    if debugger_type not in {"openocd", "stlink", "pyocd"}:
        raise ConfigError(
            "config_invalid",
            "Unsupported debugger type.",
            {"field": f"{field}.type", "value": debugger_type, "allowed_values": ["openocd", "stlink", "pyocd"]},
        )
    target: TargetConfig | None = None
    if raw.get("target") is not None:
        target_raw = mapping(raw.get("target"), f"{field}.target")
        target = TargetConfig(
            name=str(target_raw.get("name", default_target.name)),
            controller=str(target_raw.get("controller", default_target.controller)),
        )
    return debugger_config(raw, debugger_type, field, target)


def debugger_config(raw: JsonObject, debugger_type: str, field: str = "debugger", target: TargetConfig | None = None) -> DebuggerConfig:
    return DebuggerConfig(
        type=debugger_type,  # type: ignore[arg-type]
        executable=optional_string(raw.get("executable")),
        probe_id=optional_string(raw.get("probe_id")),
        target_type=optional_string(raw.get("target_type")),
        interface=str(raw.get("interface", "SWD")),
        interface_cfg=str(raw.get("interface_cfg", "interface/stlink.cfg")),
        target_cfg=str(raw.get("target_cfg", "target/stm32f4x.cfg")),
        flash_address=optional_string(raw.get("flash_address")),
        timeout_s=float(raw.get("timeout_s", 60)),
        resource_id=optional_string(raw.get("resource_id")),
        permissions=debugger_permissions(mapping(raw.get("permissions"), f"{field}.permissions")),
        target=target,
    )


def debugger_permissions(raw: JsonObject) -> DebuggerPermissions:
    return DebuggerPermissions(
        allow_probe=bool(raw.get("allow_probe", False)),
        allow_flash=bool(raw.get("allow_flash", False)),
        allow_reset=bool(raw.get("allow_reset", False)),
        allow_raw_debugger_commands=bool(raw.get("allow_raw_debugger_commands", False)),
        allow_mass_erase=bool(raw.get("allow_mass_erase", False)),
    )


def io_permissions(raw: JsonObject) -> IoPermissions:
    return IoPermissions(allow_read=bool(raw.get("allow_read", False)), allow_write=bool(raw.get("allow_write", False)))


def project_permissions(raw: JsonObject) -> ProjectPermissions:
    return ProjectPermissions(
        allow_config_write=bool(raw.get("allow_config_write", False)),
        allow_config_description_write=bool(raw.get("allow_config_description_write", False)),
        allow_config_permissions_write=bool(raw.get("allow_config_permissions_write", False)),
    )


def debugger_access_enabled(debugger: DebuggerConfig) -> bool:
    return any([debugger.permissions.allow_probe, debugger.permissions.allow_flash, debugger.permissions.allow_reset])


@cache
def _skeleton_debugger() -> JsonObject:
    """The one `debuggers` entry the shipped skeleton writes, parsed once."""
    loaded = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    entries = loaded.get("debuggers") if isinstance(loaded, dict) else None
    entry = next((value for value in entries.values() if isinstance(value, dict)), {}) if isinstance(entries, dict) else {}
    return entry


def debugger_is_placeholder(debugger: DebuggerConfig) -> bool:
    """Whether this entry is still the starter one and drives nothing as written.

    It has to be asked because hardci-hq#96 made every permission true by
    default. Two rules key on "this bench is meant to drive hardware": config
    load insists such an entry's toolchain resolves, and `doctor` probes it. A
    granted permission used to be a deliberate human edit and so a good signal
    for both. Now it is what a generation writes, so without this a starter file
    would demand an installed toolchain and an attached board the moment `init`
    produced it — and `init` would leave behind a configuration that does not
    load, on the one path this issue exists to make work.

    What is compared is what the shipped skeleton writes, and only values that
    survive pinning, so the answer is the same before and after config load
    rewrites a configured entry's paths:

    * An **OpenOCD** entry still holding both skeleton script names. They are
      relative, and the rule for a driving OpenOCD entry is that they are
      absolute and outside the workspace — so an entry that kept them cannot
      drive anything however its permissions read, whether or not
      `project_config_adopt_hardware` has since filled in its probe serial.

      Deliberately not narrowed by probe id or executable. Adoption fills a
      serial into exactly this entry without touching its scripts, and requiring
      the scripts from that point on would make the one flow this issue exists
      to make work — `init`, plug the board in, adopt — end in a configuration
      that no longer loads. What keeps the relative names harmless is not this
      predicate: OpenOCD is spawned with its working directory pinned to the
      directory of the executable itself (`spawn_command` in
      `backends/openocd.py`), so `interface/stlink.cfg` resolves inside the
      toolchain's own script tree and the workspace cannot shadow it.
    * Any other entry with no toolchain behind it: none named, or the one pinning
      substituted because nothing resolved. That marker is what makes the answer
      survive pinning, which replaces an unresolved executable with a path under
      the configuration directory that deliberately does not exist.
    """
    skeleton = _skeleton_debugger()
    if debugger.type == skeleton.get("type"):
        return debugger.interface_cfg == skeleton.get("interface_cfg") and debugger.target_cfg == skeleton.get("target_cfg")
    return debugger.executable is None or executable_is_disabled(debugger.executable)


def debugger_drives_hardware(debugger: DebuggerConfig) -> bool:
    """Whether this entry is one a toolchain has to be resolvable for.

    The single answer config load and `doctor` share, so the set `doctor` checks
    stays exactly the set load insisted on resolving."""
    return debugger_access_enabled(debugger) and not debugger_is_placeholder(debugger)


def debug_interface_config(raw: JsonObject) -> DebugInterfaceConfig:
    return DebugInterfaceConfig(
        gdb_executable=optional_string(raw.get("gdb_executable")),
        allowed_symbols=string_list(raw.get("allowed_symbols"), []),
        allow_all_symbols=bool(raw.get("allow_all_symbols", False)),
        max_dump_size_bytes=positive_integer_config(raw.get("max_dump_size_bytes"), 1024 * 1024, "debug.max_dump_size_bytes"),
    )


# One root that names ``workspace_root`` itself, so every directory under it is
# permitted. It needs no special case anywhere: it pins to the workspace and the
# ordinary containment comparison then matches every path inside it.
WHOLE_WORKSPACE_ARTIFACT_ROOT = "."
# What an omitted ``artifacts.allowed_roots`` has always meant, and still means.
# Generated configurations name WHOLE_WORKSPACE_ARTIFACT_ROOT explicitly rather
# than relying on this: changing what the *absence* of the key means would widen
# every existing file that never named it, and an update does not broaden what an
# operator set (decision 0018).
LEGACY_ARTIFACT_ROOTS = ["build"]


def artifacts_config(raw: JsonObject) -> ArtifactsConfig:
    return ArtifactsConfig(
        allowed_roots=string_list(raw.get("allowed_roots"), LEGACY_ARTIFACT_ROOTS),
        upload_directory=str(raw.get("upload_directory", ".agentic-hil/artifacts")),
        allowed_extensions=[item.lower() for item in string_list(raw.get("allowed_extensions"), [".elf", ".hex", ".bin"])],
        max_upload_size_mb=int(raw.get("max_upload_size_mb", 64)),
        allow_upload=bool(raw.get("allow_upload", False)),
    )


def com_port_config(name: str, value: Any) -> ComPortConfig:
    raw = mapping(value, f"com_ports.{name}")
    return ComPortConfig(
        device=str(raw["device"]),
        baudrate=int(raw.get("baudrate", 115200)),
        timeout_s=float(raw.get("timeout_s", 0.1)),
        write_timeout_s=float(raw.get("write_timeout_s", 1.0)),
        encoding=str(raw.get("encoding", "utf-8")),
        max_buffer_bytes=int(raw.get("max_buffer_bytes", 65536)),
        max_write_bytes=int(raw.get("max_write_bytes", 4096)),
        assert_dtr=bool(raw.get("assert_dtr", True)),
        assert_rts=bool(raw.get("assert_rts", True)),
        resource_id=optional_string(raw.get("resource_id")),
        permissions=io_permissions(mapping(raw.get("permissions"), f"com_ports.{name}.permissions")),
    )


def can_bus_config(name: str, value: Any) -> CanBusConfig:
    raw = mapping(value, f"can_buses.{name}")
    adapter = str(raw.get("adapter", "peak"))
    if adapter not in {"peak", "socketcan", "process"}:
        raise ConfigError(
            "config_invalid",
            "Unsupported can_buses adapter.",
            {"field": f"can_buses.{name}.adapter", "value": adapter, "allowed_values": ["peak", "socketcan", "process"]},
        )
    fd = bool(raw.get("fd", False))
    return CanBusConfig(
        adapter=adapter,  # type: ignore[arg-type]
        channel=str(raw["channel"]),
        bitrate=int(raw.get("bitrate", 500000)),
        fd=fd,
        data_bitrate=None if raw.get("data_bitrate") is None else int(raw["data_bitrate"]),
        pcanbasic_dll=optional_string(raw.get("pcanbasic_dll")),
        executable=optional_string(raw.get("executable")),
        args=[],
        timeout_s=float(raw.get("timeout_s", 10.0)),
        poll_interval_ms=int(raw.get("poll_interval_ms", 10)),
        receive_own_messages=bool(raw.get("receive_own_messages", False)),
        listen_only=bool(raw.get("listen_only", False)),
        max_buffer_frames=int(raw.get("max_buffer_frames", 1024)),
        max_frame_data_bytes=int(raw.get("max_frame_data_bytes", 64 if fd else 8)),
        resource_id=optional_string(raw.get("resource_id")),
        permissions=io_permissions(mapping(raw.get("permissions"), f"can_buses.{name}.permissions")),
    )


def validation_config(raw: JsonObject) -> ValidationConfig:
    return ValidationConfig(
        require_existing_file=bool(raw.get("require_existing_file", True)),
        require_allowed_root=bool(raw.get("require_allowed_root", True)),
        require_allowed_extension=bool(raw.get("require_allowed_extension", True)),
        compute_sha256=bool(raw.get("compute_sha256", True)),
        inspect_known_formats=bool(raw.get("inspect_known_formats", True)),
    )


def reports_config(raw: JsonObject) -> ReportsConfig:
    return ReportsConfig(directory=str(raw.get("directory", ".agentic-hil/reports")))


def logs_config(raw: JsonObject) -> LogsConfig:
    return LogsConfig(directory=str(raw.get("directory", ".agentic-hil/logs")))


def recovery_config(raw: JsonObject) -> RecoveryConfig:
    # Whether the key was named is recorded, not just its value: a bench that
    # never chose a policy still gets the default, but machine recovery says so
    # in the audit the first time it acts physically under it.
    return RecoveryConfig(
        auto_recover=str(raw.get("auto_recover", RecoveryConfig.auto_recover)),
        max_attempts=int(raw.get("max_attempts", RecoveryConfig.max_attempts)),
        auto_recover_explicit="auto_recover" in raw,
    )


REMOVED_SECTIONS: dict[str, tuple[str, JsonObject]] = {
    "permissions": (
        "The top-level permissions block was removed. Declare permissions on the device they authorize.",
        {
            "allow_probe": "debuggers.<name>.permissions.allow_probe",
            "allow_flash": "debuggers.<name>.permissions.allow_flash",
            "allow_reset": "debuggers.<name>.permissions.allow_reset",
            "allow_raw_debugger_commands": "debuggers.<name>.permissions.allow_raw_debugger_commands",
            "allow_mass_erase": "debuggers.<name>.permissions.allow_mass_erase",
            "allow_com_read": "com_ports.<name>.permissions.allow_read",
            "allow_com_write": "com_ports.<name>.permissions.allow_write",
            "allow_can_read": "can_buses.<name>.permissions.allow_read",
            "allow_can_write": "can_buses.<name>.permissions.allow_write",
            "allow_adapter_read": "no replacement; test adapters are not supported in this release",
            "allow_adapter_write": "no replacement; test adapters are not supported in this release",
        },
    ),
    "debugger": (
        "The single top-level debugger block was removed. Every debug probe is now a named entry under debuggers.",
        {"debugger": "debuggers.<name> — pick any name; test plans and tool calls address the probe by it"},
    ),
    "devices": (
        "The devices block was removed. Debug probes, COM ports, and CAN buses are addressed by their own names.",
        {
            "devices.<name>.debugger": "debuggers.<name> — the debugger name IS the routing key",
            "devices.<name>.uart": "com_ports.<name> — test steps and tool calls name the port directly",
            "devices.<name>.target": "debuggers.<name>.target",
        },
    ),
    "adapters": (
        "Test adapters are not supported in this release; the adapters block was removed.",
        {"adapters": "no replacement yet"},
    ),
}


# A top-level `permissions` block exists again, holding the grants that are not
# about a device. The refusal below therefore has to fire on the removed device
# grants inside it rather than on the section's name, or every file written after
# this release would be read as a pre-migration file. Every project-scoped grant
# the schema declares belongs in this set; a test compares the two, because a
# grant added to the schema and forgotten here would make every file that uses it
# unloadable with a migration error about a block from three releases ago.
SURVIVING_SECTION_KEYS = {"permissions": {"allow_config_write", "allow_config_description_write", "allow_config_permissions_write"}}


def reject_removed_sections(raw: JsonObject, config_path: str) -> None:
    """Refuse config sections this release removed.

    Schema validation alone would only say the field is unknown. A permission or
    routing model is the last thing an operator should have to guess at, so name
    where each removed key went and fail closed rather than running a config
    whose grants and device bindings are no longer read by anything."""
    for section, (summary, migration) in REMOVED_SECTIONS.items():
        if section not in raw:
            continue
        surviving = SURVIVING_SECTION_KEYS.get(section)
        value = raw[section]
        if surviving is not None and isinstance(value, dict) and set(value) <= surviving:
            continue
        raise ConfigError("config_invalid", summary, {"path": config_path, "field": section, "migration": migration})


def configured_version(raw: JsonObject, config_path: str) -> int:
    """Which permission model this file is read under.

    A file with no ``version:`` was written when reading needed a grant, and it
    keeps that meaning. Nothing infers the new model from the absence of a key:
    that inference is exactly the silent widening this marker exists to stop."""
    value = raw.get("version")
    if value is None:
        return LEGACY_CONFIG_VERSION
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("config_invalid", "version must be an integer.", {"path": config_path, "field": "version", "value": value})
    if value not in {LEGACY_CONFIG_VERSION, READ_FREE_CONFIG_VERSION}:
        raise ConfigError(
            "config_invalid",
            f"Unsupported configuration version; this release reads version {LEGACY_CONFIG_VERSION} and {READ_FREE_CONFIG_VERSION}.",
            {"path": config_path, "field": "version", "value": value, "supported_versions": [LEGACY_CONFIG_VERSION, READ_FREE_CONFIG_VERSION]},
        )
    return value


# Top-level keys this release removed, and what a file that still carries one
# has to be told. Same argument as ``reject_removed_sections``: the schema would
# only say the key is unknown, and a key that used to select a security policy is
# the last thing to leave a reader guessing about.
REMOVED_KEYS: dict[str, tuple[str, JsonObject]] = {
    "windows_path_trust": (
        "windows_path_trust was removed together with the Windows ACL and POSIX mode path trust check it selected. "
        "No path is judged by who else on the machine could write it any more, so the key selects nothing.",
        {
            "removed": "The ancestor walk and the principal classification, on both platforms. A multi-user guarantee "
            "was never a requirement of this project, and the check could never hold one: the operator owns these "
            "objects and holds FullControl on them, so every ordinary process of that user could always rewrite them.",
            "instead": "Detection rather than prevention. `config_status` in every tool result reports whether the "
            "configuration on disk is still the one this server loaded, by SHA-256 of the exact bytes, and the audit "
            "trail records the digest an action ran under.",
            "unchanged": "state_root is still created and opened before it is used, symlinked path components are "
            "still refused, and the file-level deny rules `agentic-hil setup` writes into an agent's configuration are "
            "untouched.",
            "action": "Delete the key. Nothing replaces it.",
        },
    ),
}


def reject_removed_keys(raw: JsonObject, config_path: str) -> None:
    """Refuse a top-level key this release removed, by name.

    Ignoring it silently would leave an operator believing a policy is still
    being selected by a line that nothing reads. 0.8.0 is unreleased, so no file
    in the field carries this — which is exactly why the removal is free now."""
    for key, (summary, migration) in REMOVED_KEYS.items():
        if key in raw:
            raise ConfigError("config_invalid", summary, {"path": config_path, "field": key, "migration": migration})


def reject_empty_allowed_roots(raw: JsonObject, config_path: str) -> None:
    """Refuse an empty ``artifacts.allowed_roots``, because it reads both ways.

    An empty list is one obvious way to write "no restriction" and equally the
    obvious way to write "nothing at all". Here it has always meant the second —
    ``debug.allowed_symbols: []`` denies every symbol — so a reader who writes it
    meaning the first gets the exact opposite in silence. There is one spelling
    for the whole workspace and this is not it; say so by name rather than let
    the two readings share a key."""
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, dict):
        return
    roots = artifacts.get("allowed_roots")
    if not isinstance(roots, list) or roots:
        return
    raise ConfigError(
        "config_invalid",
        "artifacts.allowed_roots must name at least one directory; an empty list is ambiguous.",
        {
            "path": config_path,
            "field": "artifacts.allowed_roots",
            "migration": {
                "whole_workspace": f'["{WHOLE_WORKSPACE_ARTIFACT_ROOT}"] permits every directory under workspace_root, recursively',
                "narrower": 'Name the directories firmware may be flashed from, for example ["build", "cmake-build-debug"]',
                "uploads_only": "Name artifacts.upload_directory to permit uploaded artifacts and nothing else",
            },
        },
    )


# The permission each section used to spell "may read", and where it went.
READ_PERMISSIONS = {
    "debuggers": "allow_probe",
    "com_ports": "allow_read",
    "can_buses": "allow_read",
}


def reject_read_permissions(raw: JsonObject, config_path: str, version: int) -> None:
    """Refuse a read permission in a version 2 file, by name.

    Version 2 has no read permission to set, so a file that still carries one is
    either half-migrated or written against the old model. Both are worth a
    refusal that names the key: silently ignoring it would leave an operator
    believing a flag still gates something."""
    if version < READ_FREE_CONFIG_VERSION:
        return
    for section, flag in READ_PERMISSIONS.items():
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            permissions = value.get("permissions") if isinstance(value, dict) else None
            if isinstance(permissions, dict) and flag in permissions:
                raise ConfigError(
                    "config_invalid",
                    f"Version {READ_FREE_CONFIG_VERSION} has no read permission: reading needs no grant, and exclusivity for the duration of a run replaced it. Remove this key.",
                    {
                        "path": config_path,
                        "field": f"{section}.{name}.permissions.{flag}",
                        "migration": {
                            "remove": f"{section}.<name>.permissions.{flag}",
                            "keep": "allow_flash, allow_reset, allow_write, allow_mass_erase and allow_raw_debugger_commands are unchanged; this migration touches the read permissions and nothing else",
                            "instead": "Declare the device in the test description; it is locked for the whole run, and a device the description does not name is refused.",
                        },
                    },
                )


def reject_bridge_args(raw: JsonObject, config_path: str) -> None:
    for section in ("can_buses",):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            if isinstance(value, dict) and "args" in value:
                raise ConfigError(
                    "config_invalid",
                    "Process bridge args are not accepted across the trusted policy boundary. Pin an operator-controlled wrapper directly as executable.",
                    {"path": config_path, "field": f"{section}.{name}.args"},
                )


def mapping(value: Any, field_name: str) -> JsonObject:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("config_invalid", f"{field_name} must be a mapping.", {"field": field_name})
    return value


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def string_list(value: Any, default_value: list[str]) -> list[str]:
    if value is None:
        return list(default_value)
    if not isinstance(value, list):
        raise ConfigError("config_invalid", "Configuration value must be a list.")
    return [str(item) for item in value]


def positive_integer_config(value: Any, default_value: int, field: str) -> int:
    parsed = int(value if value is not None else default_value)
    if parsed < 1:
        raise ConfigError("config_invalid", f"{field} must be a finite integer >= 1.", {"field": field, "value": value})
    return parsed
