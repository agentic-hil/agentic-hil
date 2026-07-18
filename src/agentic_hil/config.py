from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from agentic_hil.types import (
    AdapterConfig,
    AgenticHILConfig,
    ArtifactsConfig,
    CanBusConfig,
    ComPortConfig,
    DebuggerConfig,
    DebugInterfaceConfig,
    DeviceConfig,
    JsonObject,
    LogsConfig,
    PermissionsConfig,
    ReportsConfig,
    TargetConfig,
    ValidationConfig,
)

CONFIG_ENV = "AGENTIC_HIL_CONFIG"
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
        return {"ok": False, "error_type": self.error_type, "summary": self.summary, **self.details}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> JsonObject:
    loader.flatten_mapping(node)
    result: JsonObject = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from error
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


def config_schema_text() -> str:
    return resources.files("agentic_hil").joinpath(CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8")


def config_schema() -> JsonObject:
    return json.loads(config_schema_text())


def validate_config_schema(raw: JsonObject, config_path: str | None = None) -> None:
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


def load_config(config_path: str | None = None, work_dir: str | None = None) -> AgenticHILConfig:
    """Parse one config file. Production entrypoints must use load_authoritative_config."""
    if config_path is None:
        return load_authoritative_config(work_dir or Path.cwd())
    config_file = Path(config_path).expanduser().resolve()
    resolved_config_path = str(config_file)
    if not config_file.exists():
        raise ConfigError(
            "config_file_not_found",
            "Agentic HIL configuration file could not be found.",
            {"path": resolved_config_path},
        )

    try:
        loaded = yaml.load(config_file.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
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
        raise ConfigError(
            "config_invalid",
            "Agentic HIL configuration file is not valid YAML.",
            {"path": resolved_config_path},
        ) from error

    raw: Any = loaded or {}
    if not isinstance(raw, dict):
        raise ConfigError("config_invalid", "Agentic HIL configuration root must be a mapping.", {"path": resolved_config_path})
    if "workspace_root" not in raw:
        raise ConfigError(
            "config_migration_required",
            "Agentic HIL 0.2.3-style configurations must be migrated to the external authoritative policy format.",
            {"path": resolved_config_path, "next_step": f"agentic-hil migrate-config --from {resolved_config_path}"},
        )
    reject_legacy_bridge_args(raw, resolved_config_path)
    validate_config_schema(raw, resolved_config_path)

    workspace_value = str(raw["workspace_root"])
    workspace_requested = Path(workspace_value).expanduser()
    if not workspace_requested.is_absolute():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an absolute path.",
            {"path": resolved_config_path, "field": "workspace_root", "value": workspace_value},
        )
    workspace = workspace_requested.resolve()
    if not workspace.is_dir():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an existing directory.",
            {"path": resolved_config_path, "field": "workspace_root", "value": workspace_value},
        )
    if work_dir is not None and workspace != Path(work_dir).resolve():
        raise ConfigError(
            "config_invalid",
            "Configured workspace_root does not match the requested work directory.",
            {"workspace_root": str(workspace), "expected_workspace": str(Path(work_dir).resolve())},
        )

    target_raw = mapping(raw.get("target"), "target")
    devices_raw = mapping(raw.get("devices"), "devices")
    debugger_raw = mapping(raw.get("debugger"), "debugger")
    debug_raw = mapping(raw.get("debug"), "debug")
    artifacts_raw = mapping(raw.get("artifacts"), "artifacts")
    com_ports_raw = mapping(raw.get("com_ports"), "com_ports")
    can_buses_raw = mapping(raw.get("can_buses"), "can_buses")
    adapters_raw = mapping(raw.get("adapters"), "adapters")
    validation_raw = mapping(raw.get("validation"), "validation")
    permissions_raw = mapping(raw.get("permissions"), "permissions")
    reports_raw = mapping(raw.get("reports"), "reports")
    logs_raw = mapping(raw.get("logs"), "logs")

    debugger_type = str(debugger_raw.get("type", "openocd"))
    if debugger_type not in {"openocd", "stlink", "pyocd"}:
        raise ConfigError(
            "config_invalid",
            "Unsupported debugger.type.",
            {"field": "debugger.type", "value": debugger_type, "allowed_values": ["openocd", "stlink", "pyocd"]},
        )

    com_ports = {name: com_port_config(name, value) for name, value in com_ports_raw.items()}
    devices = {name: device_config(name, value) for name, value in devices_raw.items()}
    validate_devices(devices, com_ports)

    return AgenticHILConfig(
        config_path=resolved_config_path,
        work_dir=str(workspace),
        workspace_root=str(workspace),
        target=target_config(target_raw),
        devices=devices,
        debugger=debugger_config(debugger_raw, debugger_type),
        debug=debug_interface_config(debug_raw),
        artifacts=artifacts_config(artifacts_raw),
        com_ports=com_ports,
        can_buses={name: can_bus_config(name, value) for name, value in can_buses_raw.items()},
        adapters={name: adapter_config(name, value) for name, value in adapters_raw.items()},
        validation=validation_config(validation_raw),
        permissions=permissions_config(permissions_raw),
        reports=reports_config(reports_raw),
        logs=logs_config(logs_raw),
    )


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
    resolved = requested.resolve()
    if not resolved.is_file():
        raise ConfigError("config_file_not_found", "Agentic HIL configuration file could not be found.", {"path": str(resolved)})
    if os.lstat(resolved).st_nlink != 1:
        raise ConfigError(
            "config_invalid",
            "The authoritative config must be a single-link regular file.",
            {"path": str(resolved)},
        )
    config = load_config(str(resolved))
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
    return pin_configured_paths(pin_configured_executables(config))


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


def pin_configured_executables(config: AgenticHILConfig) -> AgenticHILConfig:
    debugger_enabled = any(
        [
            config.permissions.allow_probe,
            config.permissions.allow_flash,
            config.permissions.allow_reset,
        ]
    )
    debugger_candidates = {
        "openocd": ["openocd"],
        "stlink": ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"],
        "pyocd": ["pyocd"],
    }[config.debugger.type]
    configured_debugger = config.debugger.executable
    if configured_debugger is None and config.debugger.type == "stlink":
        from agentic_hil.backends.common import find_stm32_programmer_cli

        configured_debugger = find_stm32_programmer_cli()
    debugger_executable = configured_executable(
        config,
        configured_debugger,
        "debugger.executable",
        candidates=debugger_candidates,
        required=debugger_enabled,
    )
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
    adapters = {
        name: replace(
            adapter,
            executable=configured_executable(
                config,
                adapter.executable,
                f"adapters.{name}.executable",
                workspace_relative=True,
                required=True,
            )
            or adapter.executable,
        )
        for name, adapter in config.adapters.items()
    }
    debugger = replace(config.debugger, executable=debugger_executable)
    if debugger_enabled and debugger.type == "openocd":
        debugger = replace(
            debugger,
            interface_cfg=configured_external_file(config, debugger.interface_cfg, "debugger.interface_cfg"),
            target_cfg=configured_external_file(config, debugger.target_cfg, "debugger.target_cfg"),
        )
    return replace(
        config,
        debugger=debugger,
        debug=replace(config.debug, gdb_executable=gdb_executable),
        can_buses=can_buses,
        adapters=adapters,
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


def disabled_executable_path(config: AgenticHILConfig, field: str) -> str:
    safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", field)
    return str(Path(config.config_path).parent / f".agentic-hil-disabled-{safe_field}")


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
            "Output file leaves the workspace.",
            {"path": str(path)},
        )
    return path


def _open_directory_fd(directory: Path, *, create: bool = False) -> int:
    path = absolute_without_symlinks(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    descriptor = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            opened = os.fstat(next_descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_descriptor)
                raise ConfigError("unsafe_configured_path", "Configured path component is not a directory.", {"path": str(path)})
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except ConfigError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        _raise_unsafe_path_error(error, path)
        raise


def _raise_unsafe_path_error(error: OSError, path: Path) -> None:
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise ConfigError(
            "unsafe_configured_path",
            "Configured path contains a symlink or non-directory component.",
            {"path": str(path)},
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
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            opened = _validate_open_file(descriptor, path)
            if not os.path.samestat(opened, os.stat(path, follow_symlinks=False)):
                raise ConfigError("unsafe_configured_path", "Lock file changed while it was being opened.", {"path": str(path)})
            if opened.st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
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
    delete_access = 0x10000
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
                component.mkdir()
            handle = create_file(
                str(component),
                file_read_attributes | delete_access,
                file_share_read_write,
                None,
                open_existing,
                file_flag_open_reparse_point | file_flag_backup_semantics,
                None,
            )
            if handle == invalid_handle:
                error_code = ctypes.get_last_error()
                if error_code not in {5, 32}:
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
    except Exception:
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


def _validate_open_file(descriptor: int, path: Path) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ConfigError(
            "unsafe_configured_path",
            "Configured file must be a single-link regular file.",
            {"path": str(path)},
        )
    return opened


def safe_read_bytes(file_path: str | Path, *, workspace: str | Path | None = None) -> bytes:
    path = _validated_absolute_file_path(file_path, workspace)
    if os.name != "nt":
        parent_descriptor = _open_directory_fd(path.parent)
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
            except OSError as error:
                _raise_unsafe_path_error(error, path)
                raise
        finally:
            os.close(parent_descriptor)
        try:
            _validate_open_file(descriptor, path)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    directory_handles = _windows_hold_directory_chain(path.parent)
    try:
        path = safe_file_path(path, workspace)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    finally:
        _close_windows_handles(directory_handles)
    try:
        opened = _validate_open_file(descriptor, path)
        current = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(opened, current) or path.parent.resolve() != path.parent:
            raise ConfigError("unsafe_configured_path", "Configured file changed while it was being opened.", {"path": str(path)})
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
            lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                lock_stat = _validate_open_file(lock_descriptor, lock_path)
                if not os.path.samestat(lock_stat, os.stat(lock_path, follow_symlinks=False)):
                    raise ConfigError("unsafe_configured_path", "Append lock changed while it was being opened.", {"path": str(lock_path)})
                if os.fstat(lock_descriptor).st_size == 0:
                    os.write(lock_descriptor, b"0")
                os.lseek(lock_descriptor, 0, os.SEEK_SET)
                import msvcrt

                msvcrt.locking(lock_descriptor, msvcrt.LK_LOCK, 1)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
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
    elif isinstance(value, (int, float, bool)) or value is None:
        details["value"] = value


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


def device_config(name: str, value: Any) -> DeviceConfig:
    raw = mapping(value, f"devices.{name}")
    return DeviceConfig(debugger=bool(raw.get("debugger", False)), uart=optional_string(raw.get("uart")))


def validate_devices(devices: dict[str, DeviceConfig], com_ports: dict[str, ComPortConfig]) -> None:
    debugger_devices = [name for name, device in devices.items() if device.debugger]
    if len(debugger_devices) > 1:
        raise ConfigError(
            "config_invalid",
            "Only one device may use the globally configured debugger.",
            {"field": "devices", "debugger_devices": debugger_devices},
        )
    for name, device in devices.items():
        if device.uart is not None and device.uart not in com_ports:
            raise ConfigError(
                "config_invalid",
                "Device references an unknown UART from com_ports.",
                {"field": f"devices.{name}.uart", "value": device.uart},
            )


def debugger_config(raw: JsonObject, debugger_type: str) -> DebuggerConfig:
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
    )


def debug_interface_config(raw: JsonObject) -> DebugInterfaceConfig:
    return DebugInterfaceConfig(
        gdb_executable=optional_string(raw.get("gdb_executable")),
        allowed_symbols=string_list(raw.get("allowed_symbols"), []),
        allow_all_symbols=bool(raw.get("allow_all_symbols", False)),
        max_dump_size_bytes=positive_integer_config(raw.get("max_dump_size_bytes"), 1024 * 1024, "debug.max_dump_size_bytes"),
    )


def artifacts_config(raw: JsonObject) -> ArtifactsConfig:
    return ArtifactsConfig(
        allowed_roots=string_list(raw.get("allowed_roots"), ["build"]),
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
    )


def adapter_config(name: str, value: Any) -> AdapterConfig:
    raw = mapping(value, f"adapters.{name}")
    return AdapterConfig(
        executable=str(raw["executable"]),
        args=[],
        timeout_s=float(raw.get("timeout_s", 10.0)),
        channels=string_list(raw.get("channels"), []),
        faults=string_list(raw.get("faults"), []),
    )


def validation_config(raw: JsonObject) -> ValidationConfig:
    return ValidationConfig(
        require_existing_file=bool(raw.get("require_existing_file", True)),
        require_allowed_root=bool(raw.get("require_allowed_root", True)),
        require_allowed_extension=bool(raw.get("require_allowed_extension", True)),
        compute_sha256=bool(raw.get("compute_sha256", True)),
        inspect_known_formats=bool(raw.get("inspect_known_formats", True)),
    )


def permissions_config(raw: JsonObject) -> PermissionsConfig:
    default = False
    return PermissionsConfig(
        allow_probe=bool(raw.get("allow_probe", default)),
        allow_flash=bool(raw.get("allow_flash", default)),
        allow_reset=bool(raw.get("allow_reset", default)),
        allow_com_read=bool(raw.get("allow_com_read", default)),
        allow_com_write=bool(raw.get("allow_com_write", default)),
        allow_can_read=bool(raw.get("allow_can_read", default)),
        allow_can_write=bool(raw.get("allow_can_write", default)),
        allow_adapter_read=bool(raw.get("allow_adapter_read", default)),
        allow_adapter_write=bool(raw.get("allow_adapter_write", default)),
        allow_raw_debugger_commands=bool(raw.get("allow_raw_debugger_commands", False)),
        allow_mass_erase=bool(raw.get("allow_mass_erase", False)),
    )


def reports_config(raw: JsonObject) -> ReportsConfig:
    return ReportsConfig(directory=str(raw.get("directory", ".agentic-hil/reports")))


def logs_config(raw: JsonObject) -> LogsConfig:
    return LogsConfig(directory=str(raw.get("directory", ".agentic-hil/logs")))


def reject_legacy_bridge_args(raw: JsonObject, config_path: str) -> None:
    for section in ("can_buses", "adapters"):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            if isinstance(value, dict) and "args" in value:
                raise ConfigError(
                    "config_migration_required",
                    "Process bridge args are no longer accepted across the trusted policy boundary. Pin an operator-controlled wrapper directly as executable.",
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
