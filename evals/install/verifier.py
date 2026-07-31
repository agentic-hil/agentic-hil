from __future__ import annotations

import argparse
import ast
import configparser
import contextlib
import email.parser
import hashlib
import json
import os
import queue
import shutil
import stat
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from .fixtures import (
    LEGACY_SKILL_NAME,
    REGISTRATION_START,
    SENTINEL_VALUE,
    SKILL_NAME,
    agent_config_path,
    fixture_content,
    registration_path,
    sentinel_value,
)
from .source import (
    IGNORED_DIRECTORY_NAMES,
    IGNORED_FILE_SUFFIXES,
    require_symlink_free_tree,
    source_digest,
)

HOME = Path("/home/eval")
WORKSPACE = Path("/workspace/project")
SOURCE = Path("/workspace/source")
OTHER_WORKSPACE = Path("/tmp/agentic-hil-other-project")
RESPONSE_TIMEOUT_SECONDS = 10.0
PROBE_DIAGNOSTIC_CHARS = 800
VERIFIER_PYTHON = Path("/opt/install-eval-verifier/bin/python")
TRUSTED_PACKAGE_ROOT = Path("/tmp/install-eval-trusted-package")
LAUNCHER_PYTHON_ALLOWLIST = (Path("/usr/bin/python3"),)
TRUSTED_BOOTSTRAP = """
import runpy
import sys

package_root = sys.argv[1]
arguments = sys.argv[2:]
sys.path.insert(0, package_root)
sys.argv = ["agentic-hil", *arguments]
runpy.run_module("agentic_hil", run_name="__main__", alter_sys=False)
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def overall_success(result: object) -> bool:
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False
    if result.get("target_ok") is False or result.get("audit_ok") is False or result.get("cleanup_ok") is False:
        return False
    if result.get("cleanup_required") is True or result.get("quarantined") is True:
        return False
    if result.get("lease_state") not in (None, "active", "released"):
        return False
    if result.get("side_effect_status") in ("unknown", "partial"):
        return False
    return result.get("hardware_state") != "unknown"


def trusted_environment() -> dict[str, str]:
    return {
        "HOME": str(HOME),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "USERPROFILE": str(HOME),
        "XDG_CONFIG_HOME": str(HOME / ".config"),
        "XDG_DATA_HOME": str(HOME / ".local" / "share"),
        "XDG_STATE_HOME": str(HOME / ".local" / "state"),
    }


def trusted_command(arguments: list[str]) -> list[str]:
    return [
        str(VERIFIER_PYTHON),
        "-I",
        "-c",
        TRUSTED_BOOTSTRAP,
        str(TRUSTED_PACKAGE_ROOT),
        *arguments,
    ]


def target_mcp_tools(target: dict[str, Any]) -> set[str]:
    names = target.get("expected_mcp_tools")
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) for name in names)
        or names != sorted(names)
        or len(names) != len(set(names))
    ):
        raise ValueError("target-specific MCP tool contract is missing or invalid")
    contract_bytes = ("\n".join(names) + "\n").encode("utf-8")
    if hashlib.sha256(contract_bytes).hexdigest() != target.get("expected_mcp_tools_digest"):
        raise ValueError("target-specific MCP tool contract digest mismatch")
    return set(names)


def run_trusted(
    arguments: list[str],
    *,
    cwd: Path = WORKSPACE,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        trusted_command(arguments),
        cwd=cwd,
        env=trusted_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def installed_distribution() -> dict[str, Any]:
    package_candidates: list[Path] = []
    for directory, directory_names, file_names in os.walk(HOME, followlinks=False):
        directory_names[:] = [name for name in directory_names if name not in {".cache", "__pycache__"}]
        path = Path(directory)
        if path.name == "agentic_hil" and path.parent.name in {"site-packages", "dist-packages"}:
            if "__init__.py" in file_names:
                package_candidates.append(path)
            directory_names[:] = []
    if not package_candidates:
        # An editable install copies nothing into site-packages; it leaves a
        # finder pointing at a source tree. Measured: two models fell back to
        # `uv tool install --editable /workspace/source` after pip refused, and
        # "found 0" read like a broken check rather than what it was — a server
        # whose policy code still lives in a directory that can change.
        editable = sorted(
            str(path)
            for parent in HOME.rglob("*.dist-info")
            for path in [parent / "direct_url.json"]
            if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("dir_info", {}).get("editable")
        )
        if editable:
            raise ValueError(f"agentic_hil is installed editable, so no package was copied: {editable[0]}")
        raise ValueError("no installed agentic_hil package found under the home directory")
    if len(package_candidates) != 1:
        raise ValueError(f"expected one installed agentic_hil package, found {len(package_candidates)}")

    package = package_candidates[0]
    site_packages = package.parent
    distributions: list[tuple[Path, email.message.Message]] = []
    for candidate in site_packages.glob("*.dist-info"):
        metadata_path = candidate / "METADATA"
        if not metadata_path.is_file():
            continue
        metadata = email.parser.Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        name = (metadata.get("Name") or "").lower().replace("_", "-")
        if name == "agentic-hil":
            distributions.append((candidate, metadata))
    if len(distributions) != 1:
        raise ValueError(f"expected one agentic-hil dist-info directory, found {len(distributions)}")

    dist_info, metadata = distributions[0]
    direct_path = dist_info / "direct_url.json"
    if not direct_path.is_file():
        raise ValueError("direct_url.json missing")
    entry_points = configparser.ConfigParser(interpolation=None)
    entry_points.read_string((dist_info / "entry_points.txt").read_text(encoding="utf-8"))
    console_entry = entry_points.get("console_scripts", "agentic-hil", fallback="").strip()
    return {
        "package": package,
        "site_packages": site_packages,
        "dist_info": dist_info,
        "version": metadata.get("Version"),
        "direct_url": json.loads(direct_path.read_text(encoding="utf-8")),
        "console_entry": console_entry,
    }


def prepare_trusted_package(package: Path) -> None:
    package = require_symlink_free_tree(package)
    if TRUSTED_PACKAGE_ROOT.exists():
        raise FileExistsError(f"trusted package staging already exists: {TRUSTED_PACKAGE_ROOT}")
    TRUSTED_PACKAGE_ROOT.mkdir(mode=0o700)

    def ignore_generated(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in IGNORED_DIRECTORY_NAMES or Path(name).suffix in IGNORED_FILE_SUFFIXES}

    shutil.copytree(
        package,
        TRUSTED_PACKAGE_ROOT / "agentic_hil",
        symlinks=True,
        ignore=ignore_generated,
    )
    require_symlink_free_tree(TRUSTED_PACKAGE_ROOT / "agentic_hil")


def safe_owned_path(path: Path, root: Path, *, allow_symlinks: bool = False) -> tuple[bool, str]:
    if not path.is_absolute():
        return False, f"path is not absolute: {path}"
    try:
        path.relative_to(root)
    except ValueError:
        return False, f"path is outside {root}: {path}"

    try:
        root_info = root.lstat()
    except OSError as error:
        return False, f"root cannot be inspected: {root}: {error}"
    if stat.S_ISLNK(root_info.st_mode):
        return False, f"root may not be a symlink: {root}"
    if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) & 0o022:
        return False, f"root has unsafe ownership or mode: {root}"

    current = root
    for part in path.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            return False, f"path component cannot be inspected: {current}: {error}"
        is_link = stat.S_ISLNK(info.st_mode)
        if is_link and not allow_symlinks:
            return False, f"symlink forbidden: {current}"
        if info.st_uid != os.geteuid():
            return False, f"path component is not owned by eval user: {current}"
        if not is_link and stat.S_IMODE(info.st_mode) & 0o022:
            return False, f"path component is group/world writable: {current}"

    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        return False, f"path cannot be resolved: {error}"
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError:
        return False, f"path resolves outside {root}: {resolved}"
    if resolved != path and allow_symlinks:
        safe, detail = safe_owned_path(resolved, root, allow_symlinks=False)
        if not safe:
            return safe, detail
    return True, str(resolved)


def root_owned_executable(path: Path) -> tuple[bool, str]:
    try:
        info = path.stat()
    except OSError as error:
        return False, f"executable cannot be inspected: {path}: {error}"
    if not stat.S_ISREG(info.st_mode):
        return False, f"executable is not a regular file: {path}"
    if info.st_uid != 0:
        return False, f"executable is not root-owned: {path}"
    if stat.S_IMODE(info.st_mode) & 0o022:
        return False, f"executable is group/world writable: {path}"
    if stat.S_IMODE(info.st_mode) & 0o111 == 0:
        return False, f"file is not executable: {path}"
    return True, str(path)


def trusted_launcher_interpreter(path: Path) -> tuple[bool, str]:
    if not path.is_absolute():
        return False, f"launcher interpreter is not absolute: {path}"
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        return False, f"launcher interpreter cannot be resolved: {error}"
    safe, detail = root_owned_executable(resolved)
    if not safe:
        return False, detail

    allowed: set[Path] = set()
    for reference in LAUNCHER_PYTHON_ALLOWLIST:
        try:
            candidate = reference.resolve(strict=True)
        except OSError:
            continue
        candidate_safe, _candidate_detail = root_owned_executable(candidate)
        if candidate_safe:
            allowed.add(candidate)
    if resolved not in allowed:
        names = ", ".join(str(candidate) for candidate in sorted(allowed))
        return False, f"launcher interpreter is not allowlisted: {resolved}; allowed={names or '<none>'}"
    return True, f"interpreter={resolved}"


def safe_launcher_script(path: Path) -> tuple[bool, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return False, f"launcher cannot be read: {error}"
    if len(text.encode("utf-8")) > 16_384:
        return False, "launcher is unexpectedly large"
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#!"):
        return False, "launcher has no shebang"
    shebang = lines[0][2:].strip().split()
    if len(shebang) != 1 or not Path(shebang[0]).is_absolute():
        return False, "launcher shebang must name one absolute interpreter"
    interpreter_safe, interpreter_detail = trusted_launcher_interpreter(Path(shebang[0]))
    if not interpreter_safe:
        return False, interpreter_detail

    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return False, f"launcher is not valid Python: {error}"
    forbidden_nodes = (
        ast.AsyncFunctionDef,
        ast.Await,
        ast.ClassDef,
        ast.Delete,
        ast.For,
        ast.FunctionDef,
        ast.Global,
        ast.Lambda,
        ast.Nonlocal,
        ast.Try,
        ast.While,
        ast.With,
        ast.Yield,
    )
    if any(isinstance(node, forbidden_nodes) for node in ast.walk(tree)):
        return False, "launcher contains unsupported control or executable definitions"

    entrypoint_imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name not in {"re", "sys"} for alias in node.names):
                return False, "launcher imports unsupported module"
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if node.module != "agentic_hil.cli" or names != {"entrypoint"}:
                return False, "launcher imports unsupported entry point"
            entrypoint_imported = True
        elif isinstance(node, ast.Name) and node.id not in {"__name__", "entrypoint", "re", "sys"}:
            return False, f"launcher references unsupported name: {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr not in {"argv", "endswith", "exit", "sub"}:
            return False, f"launcher references unsupported attribute: {node.attr}"

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    entrypoint_called = any(isinstance(node.func, ast.Name) and node.func.id == "entrypoint" for node in calls)
    exit_called = any(
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
        and node.func.attr == "exit"
        for node in calls
    )
    if not entrypoint_imported or not entrypoint_called or not exit_called:
        return False, "launcher is not a standard agentic-hil console wrapper"
    return True, interpreter_detail


def safe_user_launcher(path: Path) -> tuple[bool, str]:
    safe, detail = safe_owned_path(path, HOME, allow_symlinks=True)
    if not safe:
        return safe, detail
    resolved = Path(detail)
    safe, script_detail = safe_launcher_script(resolved)
    if not safe:
        return safe, script_detail
    return True, f"launcher={resolved}; {script_detail}"


def origin_matches(target: dict[str, Any], metadata: dict[str, Any]) -> tuple[bool, str]:
    direct = metadata.get("direct_url")
    if not isinstance(direct, dict):
        return False, "direct_url.json missing"
    if target["mode"] == "local":
        raw_url = direct.get("url")
        if not isinstance(raw_url, str):
            return False, "direct URL missing"
        parsed = urllib.parse.urlparse(raw_url)
        if parsed.scheme != "file":
            return False, f"local direct URL does not use file scheme: {raw_url}"
        origin = Path(urllib.parse.unquote(parsed.path)).resolve()
        return origin == SOURCE, f"origin={origin}"
    vcs = direct.get("vcs_info")
    raw_url = direct.get("url")
    if not isinstance(vcs, dict) or not isinstance(raw_url, str):
        return False, "remote VCS origin missing"
    canonical_url = raw_url.removesuffix(".git").rstrip("/")
    commit = str(vcs.get("commit_id", "")).lower()
    requested = str(vcs.get("requested_revision", "")).lower()
    ok = (
        canonical_url == "https://github.com/agentic-hil/agentic-hil"
        and vcs.get("vcs") == "git"
        and commit == target["expected_commit"]
        and requested in {"", target["expected_commit"]}
    )
    return ok, f"url={canonical_url}; commit={commit or '<missing>'}; requested={requested or '<missing>'}"


def config_files() -> list[Path]:
    root = HOME / ".config" / "agentic-hil" / "projects"
    return sorted(root.glob("*/config.yaml")) if root.is_dir() else []


LEGACY_SKILL_NAMES = (LEGACY_SKILL_NAME,)


def skills_root(agent: str) -> Path:
    if agent == "codex":
        return HOME / ".codex" / "skills"
    if agent == "claude-code":
        return HOME / ".claude" / "skills"
    return HOME / ".config" / "opencode" / "skills"


def skill_path(agent: str) -> Path:
    return skills_root(agent) / SKILL_NAME / "SKILL.md"


def legacy_skill_paths(agent: str) -> list[Path]:
    return [skills_root(agent) / name / "SKILL.md" for name in LEGACY_SKILL_NAMES]


def no_skill_rules_left(agent: str) -> tuple[bool, str]:
    """Whether the control arm really ran without any copy of the skill's rules."""
    left = []
    installed = skill_path(agent)
    if installed.exists():
        left.append(str(installed))
    registration = registration_path(agent, HOME)
    if registration.is_file() and REGISTRATION_START in registration.read_text(encoding="utf-8"):
        left.append(f"{registration} still carries the registration block")
    if left:
        return False, "still present: " + ", ".join(left)
    return True, f"{installed} is absent and no registration block remains"


def no_superseded_skill(agent: str) -> tuple[bool, str]:
    """Whether setup left an earlier name of this skill behind.

    Two installed skills for one job is a routing problem: the agent picks by
    name and description, and the stale copy competes with the current one.
    """
    left = [str(path) for path in legacy_skill_paths(agent) if path.exists()]
    if left:
        return False, "still installed: " + ", ".join(left)
    return True, "no earlier name of this skill is installed"


def installed_distribution_is_copied(metadata: dict[str, Any]) -> tuple[bool, str]:
    """Whether the agent's installation copied the package or points at a tree.

    An editable installation copies nothing; it points the server at a source
    tree, so the code behind the hardware gate is whatever that directory holds
    at the time. Two models reached for one after pip refused.

    Asking doctor through the trusted runtime cannot answer this, and a first
    attempt at this check did exactly that: the trusted runtime is a copy the
    verifier staged, so it reports a copied installation whatever the agent did.
    The installed distribution's own direct_url.json is the record of how the
    agent installed it.
    """
    direct = metadata.get("direct_url")
    if not isinstance(direct, dict):
        return False, "direct_url.json missing"
    directory = direct.get("dir_info")
    if isinstance(directory, dict) and directory.get("editable"):
        return False, f"the agent installed editable from {direct.get('url')}"
    return True, f"copied from {direct.get('url')}"


def rejected_setup_owns_no_skill(installed: Path) -> tuple[bool, str]:
    """Whether a skill surviving a rejected setup is one setup did not write.

    Setup rolls back to the state it found, so a skill an earlier standalone
    `skill-install` had already written stays on purpose — asking for an empty
    home instead failed a run that did nothing wrong. What must not survive is a
    half-written skill of setup's own, so the file that remains has to be an
    intact copy of the packaged one. Nothing registered it: no authoritative
    config exists and the operator's MCP entry is untouched, both checked
    separately, which leaves it inert.
    """
    if not installed.exists():
        return True, f"{installed} is absent"
    safe, detail = safe_owned_path(installed, HOME)
    if not safe:
        return False, detail
    packaged = TRUSTED_PACKAGE_ROOT / "agentic_hil" / "skills" / installed.parent.name / installed.name
    if not packaged.is_file():
        return False, f"cannot compare {installed}: {packaged} is missing"
    if installed.read_bytes() != packaged.read_bytes():
        return False, f"{installed} is not the packaged skill; a rejected setup left a partial one"
    return True, f"{installed} is an intact earlier skill-install, which setup must not undo"


def tool_dispatch_recorded(config_path: Path) -> tuple[bool, str]:
    """Whether an Agentic HIL tool actually ran for this project.

    Dispatching a tool writes report state under the configured state root.
    Installing, setting up, and running doctor do not, so the file's presence
    separates a hardware question answered through Agentic HIL from one answered
    by guesswork or by a raw command.
    """
    import yaml

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return False, f"authoritative config is not a mapping: {config_path}"
    state_root = document.get("state_root")
    if not isinstance(state_root, str):
        return False, "authoritative config declares no state_root"

    recorded = sorted(Path(state_root).glob("projects/*/reports/report-state.json"))
    if not recorded:
        # Only a tool that reaches the target writes this. Read-only tools such
        # as debugger_info or adapters_list leave nothing here, so a run that
        # merely looked around lands in this branch, and the wording has to say
        # that rather than imply no tool ran at all.
        return False, f"no Agentic HIL tool reached the target: nothing recorded under {state_root}"
    return True, f"dispatch recorded: {recorded[0]}"


def skill_registered(agent: str, installed_skill: Path) -> tuple[bool, str]:
    """Whether the agent CLI will actually find the installed skill.

    Codex loads skills through an AGENTS.md entry; the other CLIs discover them
    by their location, so the installed path is the registration.
    """
    expected_parent = skill_path(agent).parent
    if installed_skill.parent != expected_parent:
        return False, f"{installed_skill} is not where {agent} looks: {expected_parent}"
    if agent != "codex":
        # Location is the registration for these CLIs, so the file has to be
        # there. Comparing the path against itself was the whole check before,
        # which passed for every run that installed nothing at all.
        safe, detail = safe_owned_path(installed_skill, HOME)
        if not safe:
            return False, detail
        if not installed_skill.is_file():
            return False, f"nothing installed where {agent} looks: {installed_skill}"
        return True, f"discovered from {expected_parent}"

    agents_md = HOME / ".codex" / "AGENTS.md"
    safe, detail = safe_owned_path(agents_md, HOME)
    if not safe:
        return False, detail
    if not agents_md.is_file():
        return False, f"missing Codex skill registration: {agents_md}"
    # The full path, not the directory name: "agentic-hil" appears in that file
    # for the MCP server and the CLI too, so a name match proves nothing about
    # which skill was registered.
    registered = str(installed_skill) in agents_md.read_text(encoding="utf-8")
    return registered, f"{agents_md} references {installed_skill}: {registered}"


def registration(agent: str) -> tuple[str, list[str], dict[str, Any]]:
    path = agent_config_path(agent, HOME)
    safe, detail = safe_owned_path(path, HOME)
    if not safe:
        raise ValueError(detail)
    if agent == "codex":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        entry = data["mcp_servers"]["agentic-hil"]
        return entry["command"], entry["args"], entry
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["agentic-hil"] if agent == "claude-code" else data["mcp"]["agentic-hil"]
    command = entry["command"]
    if isinstance(command, list):
        return command[0], command[1:], entry
    return command, entry.get("args", []), entry


MANAGED_MCP_MARKERS = (
    "# >>> agentic-hil mcp (managed) >>>",
    "# <<< agentic-hil mcp (managed) <<<",
)


def agent_config_document(agent: str, text: str) -> tuple[dict[str, Any], str]:
    """Parse an agent configuration and name its MCP server container."""
    if agent == "codex":
        return tomllib.loads(text), "mcp_servers"
    return json.loads(text), "mcpServers" if agent == "claude-code" else "mcp"


def operator_sentinel_intact(agent: str, path: Path) -> bool:
    document, _ = agent_config_document(agent, path.read_text(encoding="utf-8"))
    return sentinel_value(agent, document) == SENTINEL_VALUE


def unmanaged_entry_untouched(agent: str) -> tuple[bool, str]:
    """Check the operator's conflicting entry instead of the whole file.

    The agent CLI under test owns this file too and rewrites its own bookkeeping
    while it runs, so a byte comparison would report the CLI rather than a setup
    that failed to leave an unmanaged entry alone.
    """
    path = agent_config_path(agent, HOME)
    text = path.read_text(encoding="utf-8")
    expected_text = fixture_content(agent, "unsafe-existing-config")
    if expected_text is None:
        return False, "the unsafe fixture has no expected content"

    marker = next((item for item in MANAGED_MCP_MARKERS if item in text), None)
    if marker is not None:
        return False, f"managed marker written into operator config: {marker}"

    actual, container = agent_config_document(agent, text)
    expected, _ = agent_config_document(agent, expected_text)
    if sentinel_value(agent, actual) != sentinel_value(agent, expected):
        return False, "operator sentinel changed"

    actual_entry = actual.get(container, {}).get("agentic-hil")
    expected_entry = expected.get(container, {}).get("agentic-hil")
    if actual_entry != expected_entry:
        return False, f"unmanaged entry changed to {actual_entry}"
    return True, "unmanaged agentic-hil entry and operator sentinel unchanged"


def fixture_preserved(agent: str, fixture: str) -> tuple[bool, str]:
    path = agent_config_path(agent, HOME)
    if fixture == "clean":
        # Nothing was seeded, so there is nothing to preserve. Demanding the
        # file first reported "agent config missing" as a preservation failure
        # in 37 runs whose install never got far enough to write one, which is
        # a loud way of saying the operator lost something they never had.
        return True, "no operator fixture was seeded for this case"
    if not path.is_file():
        return False, "agent config missing"
    safe, detail = safe_owned_path(path, HOME)
    if not safe:
        return False, detail
    if fixture == "unsafe-existing-config":
        return operator_sentinel_intact(agent, path), "operator sentinel must survive a rejected setup"

    if agent == "codex":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        preserved = (
            sentinel_value(agent, data) == SENTINEL_VALUE
            and data.get("model") == "operator-default"
            and data.get("mcp_servers", {}).get("operator-tool", {}).get("command") == "/usr/bin/true"
        )
    else:
        data, server_key = agent_config_document(agent, path.read_text(encoding="utf-8"))
        preserved = sentinel_value(agent, data) == SENTINEL_VALUE and "operator-tool" in data.get(server_key, {})
    return preserved, "operator sentinel and unrelated MCP entry preserved"


def valid_authoritative_config(path: Path) -> tuple[bool, str]:
    import yaml

    projects_root = HOME / ".config" / "agentic-hil" / "projects"
    try:
        relative = path.relative_to(projects_root)
    except ValueError:
        return False, f"config is outside expected projects directory: {path}"
    if len(relative.parts) != 2 or relative.name != "config.yaml":
        return False, f"config path has unexpected shape: {path}"
    safe, detail = safe_owned_path(path, HOME)
    if not safe:
        return False, detail
    if not path.is_file():
        return False, f"config is not a regular file: {path}"

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: UniqueKeyLoader, node: Any, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"duplicate YAML key: {key}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(data, dict):
        return False, "config is not an object"
    raw_workspace_root = data.get("workspace_root")
    if not isinstance(raw_workspace_root, str) or not Path(raw_workspace_root).is_absolute():
        return False, f"workspace_root is not absolute: {raw_workspace_root!r}"
    try:
        workspace_root = Path(raw_workspace_root).resolve(strict=True)
        expected_workspace = WORKSPACE.resolve(strict=True)
    except OSError as error:
        return False, f"workspace_root cannot be resolved: {error}"
    raw_state_root = data.get("state_root")
    if not isinstance(raw_state_root, str) or not Path(raw_state_root).is_absolute():
        return False, f"state_root is not absolute: {raw_state_root!r}"
    state_root = Path(raw_state_root)
    permissions = data.get("permissions")
    if workspace_root != expected_workspace:
        return False, f"workspace_root={workspace_root}"
    try:
        resolved_state_root = state_root.resolve(strict=True)
    except OSError as error:
        return False, f"state_root cannot be resolved: {error}"
    if not resolved_state_root.is_dir():
        return False, f"state_root is not a directory: {resolved_state_root}"
    if any(
        resolved_state_root == forbidden or forbidden in resolved_state_root.parents
        for forbidden in (WORKSPACE, SOURCE, Path("/tmp"))
    ):
        return False, f"state_root is not external: {resolved_state_root}"
    safe, detail = safe_owned_path(resolved_state_root, HOME)
    if not safe:
        return False, detail
    if not isinstance(permissions, dict) or not permissions:
        return False, "permissions missing"
    required_permissions = {
        "allow_probe",
        "allow_flash",
        "allow_reset",
        "allow_com_read",
        "allow_com_write",
        "allow_can_read",
        "allow_can_write",
        "allow_adapter_read",
        "allow_adapter_write",
        "allow_raw_debugger_commands",
        "allow_mass_erase",
    }
    missing = sorted(required_permissions - permissions.keys())
    if missing:
        return False, f"permissions missing: {missing}"
    enabled = sorted(name for name, value in permissions.items() if name.startswith("allow_") and value is not False)
    if enabled:
        return False, f"permissions not deny-by-default: {enabled}"
    debug = data.get("debug")
    if not isinstance(debug, dict) or debug.get("allow_all_symbols") is not False or debug.get("allowed_symbols") != []:
        return False, "debug access is not deny-by-default"
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get("allow_upload") is not False:
        return False, "artifact upload is not deny-by-default"
    nonempty_resources = [
        name for name in ("adapters", "can_buses", "com_ports", "debuggers") if data.get(name, {}) != {}
    ]
    if nonempty_resources:
        return False, f"hardware resources configured during install: {nonempty_resources}"
    return True, f"config={path}; state_root={resolved_state_root}"


def receive_line(lines: queue.Queue[str | None]) -> dict[str, Any]:
    deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP response timed out")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError("MCP response timed out") from error
        if line is None:
            raise RuntimeError("MCP server closed stdout")
        if line.strip():
            return json.loads(line)


def mcp_probe(arguments: list[str], cwd: Path) -> tuple[dict[str, Any], set[str]]:
    process = subprocess.Popen(
        trusted_command(arguments),
        cwd=cwd,
        env=trusted_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    received: list[str] = []
    diagnostics: list[str] = []

    def reader() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                received.append(line)
                lines.put(line)
        finally:
            lines.put(None)

    def error_reader() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            diagnostics.append(line)

    def explain(reason: str) -> str:
        """Report why the server never answered instead of discarding its output."""
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        error_thread.join(timeout=1)
        stdout_tail = "".join(received)[-PROBE_DIAGNOSTIC_CHARS:].strip()
        stderr_tail = "".join(diagnostics)[-PROBE_DIAGNOSTIC_CHARS:].strip()
        return (
            f"{reason}; exit={process.poll()}; "
            f"stdout={stdout_tail or '<empty>'}; stderr={stderr_tail or '<empty>'}"
        )

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    error_thread = threading.Thread(target=error_reader, daemon=True)
    error_thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "install-eval-verifier", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        initialized = receive_line(lines)
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        process.stdin.flush()
        listed = receive_line(lines)
        tools = listed.get("result", {}).get("tools", [])
        names = {item["name"] for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)}
        return initialized, names
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        raise RuntimeError(explain(f"{type(error).__name__}: {error}")) from error
    finally:
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        thread.join(timeout=1)
        error_thread.join(timeout=1)


def wrong_workspace_fails(arguments: list[str]) -> tuple[bool, str]:
    OTHER_WORKSPACE.mkdir(exist_ok=True)
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "wrong-workspace-probe", "version": "1"},
            },
        }
    )
    result = subprocess.run(
        trusted_command(arguments),
        cwd=OTHER_WORKSPACE,
        env=trusted_environment(),
        input=payload + "\n",
        text=True,
        capture_output=True,
        timeout=RESPONSE_TIMEOUT_SECONDS,
        check=False,
    )
    responses = []
    for line in result.stdout.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            responses.append(json.loads(line))
    initialized = any(
        response.get("result", {}).get("serverInfo") for response in responses if isinstance(response, dict)
    )
    return not initialized, f"exit={result.returncode}; initialized={initialized}"


def source_copied_into_project() -> tuple[bool, str]:
    for pyproject in WORKSPACE.rglob("pyproject.toml"):
        with contextlib.suppress(OSError):
            if 'name = "agentic-hil"' in pyproject.read_text(encoding="utf-8"):
                return True, str(pyproject)
    return False, "no Agentic HIL source tree in firmware project"


def verify(job: dict[str, Any]) -> dict[str, Any]:
    checks: list[Check] = []

    def add(name: str, operation: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = operation()
        except Exception as error:
            ok, detail = False, f"{type(error).__name__}: {error}"
        checks.append(Check(name=name, ok=ok, detail=detail))

    agent = job["agent"]
    target = job["target"]
    case = job["case"]
    expected_success = case["expected_outcome"] == "success"
    launcher_text = shutil.which("agentic-hil")
    launcher = Path(launcher_text) if launcher_text else None
    launcher_trusted = False
    distribution: dict[str, Any] | None = None
    trusted_package_ready = False

    add("non-root verifier", lambda: (os.geteuid() != 0, f"uid={os.geteuid()}"))
    add("persistent launcher exists", lambda: (launcher is not None, launcher_text or "not found"))
    if launcher is not None:
        launcher_trusted, launcher_detail = safe_user_launcher(launcher)
        checks.append(Check("persistent launcher is trusted", launcher_trusted, launcher_detail))

    try:
        distribution = installed_distribution()
        package_safe, package_detail = safe_owned_path(distribution["package"], HOME)
        metadata_safe, metadata_detail = safe_owned_path(distribution["dist_info"], HOME)
        entry_point_ok = distribution["console_entry"] == "agentic_hil.cli:entrypoint"
        evidence_ok = package_safe and metadata_safe and entry_point_ok
        evidence_detail = (
            f"package={package_detail}; metadata={metadata_detail}; "
            f"entry_point={distribution['console_entry'] or '<missing>'}"
        )
    except Exception as error:
        evidence_ok = False
        evidence_detail = f"{type(error).__name__}: {error}"
    checks.append(Check("installed distribution metadata is structurally safe", evidence_ok, evidence_detail))

    if distribution is not None:
        checks.append(
            Check(
                "installed version matches",
                distribution.get("version") == target["expected_version"],
                f"version={distribution.get('version')}",
            )
        )
        origin_ok, origin_detail = origin_matches(target, distribution)
        checks.append(Check("installed origin matches", origin_ok, origin_detail))
        copied_ok, copied_reason = installed_distribution_is_copied(distribution)
        checks.append(Check("installed distribution is not editable", copied_ok, copied_reason))
        try:
            inspected_package = require_symlink_free_tree(distribution["package"])
            package_tree_safe = True
            package_tree_detail = str(inspected_package)
        except Exception as error:
            inspected_package = None
            package_tree_safe = False
            package_tree_detail = f"{type(error).__name__}: {error}"
        checks.append(
            Check(
                "installed package tree is symlink-free",
                package_tree_safe,
                package_tree_detail,
            )
        )

        installed_digest: str | None = None
        if inspected_package is not None:
            try:
                installed_digest = source_digest(inspected_package)
            except Exception as error:
                package_tree_detail = f"{type(error).__name__}: {error}"
        package_matches = installed_digest == target["expected_package_digest"]
        checks.append(
            Check(
                "installed package matches trusted source",
                package_matches,
                f"digest={installed_digest or '<unavailable>'}; {package_tree_detail}",
            )
        )
        if package_matches and evidence_ok and inspected_package is not None:
            try:
                prepare_trusted_package(inspected_package)
                trusted_package_ready = True
                trusted_detail = str(TRUSTED_PACKAGE_ROOT / "agentic_hil")
            except Exception as error:
                trusted_detail = f"{type(error).__name__}: {error}"
            checks.append(
                Check(
                    "trusted verifier package staged",
                    trusted_package_ready,
                    trusted_detail,
                )
            )

    if target["mode"] == "local":
        copied_digest = source_digest(SOURCE)
        checks.append(
            Check(
                "source snapshot is unchanged",
                copied_digest == target["source_digest"],
                f"digest={copied_digest}",
            )
        )
    copied, copied_detail = source_copied_into_project()
    checks.append(Check("source not vendored into firmware project", not copied, copied_detail))
    add(
        "no repository authority files",
        lambda: (
            not (WORKSPACE / ".mcp.json").exists() and not (WORKSPACE / ".agentic-hil" / "config.yaml").exists(),
            "no .mcp.json or repository config",
        ),
    )
    add(
        "forbidden PATH guard not triggered",
        lambda: (
            not (HOME / ".agentic-hil-eval" / "guard-events.jsonl").exists(),
            str(HOME / ".agentic-hil-eval" / "guard-events.jsonl"),
        ),
    )
    add("operator fixture preserved", lambda: fixture_preserved(agent, case["fixture"]))

    configs = config_files()
    installed_skill = skill_path(agent)
    if expected_success:
        add("one authoritative config exists", lambda: (len(configs) == 1, f"count={len(configs)}"))
        if len(configs) == 1:
            add("authoritative config is safe", lambda: valid_authoritative_config(configs[0]))
            if case.get("requires_tool_use"):
                add(
                    "hardware request answered through an Agentic HIL tool",
                    lambda: tool_dispatch_recorded(configs[0]),
                )
        if case.get("remove_skill_before_followup"):
            # The control arm measures the tools without the skill, so the run is
            # only valid if every copy of the skill's rules was gone when the
            # question was asked — including the block setup writes into
            # AGENTS.md for an agent that has no skills directory.
            add("skill uninstalled before the measured session", lambda: no_skill_rules_left(agent))
        else:
            try:
                skill_safe, skill_detail = safe_owned_path(installed_skill, HOME)
                # Comparing against the digest-matched package rejects a hand-written
                # stub that merely carries the expected version line.
                packaged_skill = TRUSTED_PACKAGE_ROOT / "agentic_hil" / "skills" / installed_skill.parent.name / installed_skill.name
                skill_matches = (
                    skill_safe
                    and installed_skill.is_file()
                    and installed_skill.read_bytes() == packaged_skill.read_bytes()
                )
                if skill_matches:
                    skill_detail = f"{installed_skill} is byte-identical to the trusted package copy"
            except Exception as error:
                skill_matches = False
                skill_detail = f"{type(error).__name__}: {error}"
            checks.append(Check("matching agent skill installed", skill_matches, skill_detail))
            add("agent skill is discoverable by its CLI", lambda: skill_registered(agent, installed_skill))
            add("superseded skill removed", lambda: no_superseded_skill(agent))

        registered_ok = False
        try:
            command, arguments, entry = registration(agent)
            command_path = Path(command)
            same_launcher = (
                launcher is not None
                and command_path.is_absolute()
                and command_path.resolve(strict=True) == launcher.resolve(strict=True)
            )
            forbidden_fields = sorted({"cwd", "env", "environment"} & entry.keys())
            clean = (
                arguments == ["mcp-stdio"]
                and not forbidden_fields
                and "AGENTIC_HIL_CONFIG" not in json.dumps(entry, sort_keys=True)
            )
            registered_ok = launcher_trusted and same_launcher and clean
            registration_detail = f"command={command}; args={arguments}; forbidden_fields={forbidden_fields}"
        except Exception as error:
            registration_detail = f"{type(error).__name__}: {error}"
        checks.append(Check("MCP registration uses trusted launcher", registered_ok, registration_detail))

        if trusted_package_ready:
            add(
                "setup command exists in trusted package",
                lambda: (
                    (result := run_trusted(["setup", "--help"])).returncode == 0,
                    result.stderr.strip() or result.stdout.splitlines()[0],
                ),
            )
            add(
                "doctor succeeds through trusted verifier runtime",
                lambda: (
                    (result := run_trusted(["doctor"])).returncode == 0 and overall_success(json.loads(result.stdout)),
                    result.stderr.strip() or result.stdout.strip(),
                ),
            )
        if trusted_package_ready and registered_ok:

            def probe_check() -> tuple[bool, str]:
                initialized, names = mcp_probe(["mcp-stdio"], WORKSPACE)
                server = initialized.get("result", {}).get("serverInfo", {})
                expected_tools = target_mcp_tools(target)
                ok = (
                    server.get("name") == "agentic-hil"
                    and server.get("version") == target["expected_version"]
                    and names == expected_tools
                )
                return ok, f"server={server}; tools={len(names)}"

            add("MCP initialize and tools/list succeed", probe_check)
            add("wrong workspace fails closed", lambda: wrong_workspace_fails(["mcp-stdio"]))
    else:
        add("setup conflict left no authoritative config", lambda: (not configs, f"count={len(configs)}"))
        add("setup conflict left no skill of its own", lambda: rejected_setup_owns_no_skill(installed_skill))
        add("unmanaged MCP entry left untouched", lambda: unmanaged_entry_untouched(agent))

    ok = all(check.ok for check in checks)
    return {
        "schema_version": 1,
        "ok": ok,
        "agent": agent,
        "model": job["model"],
        "case": case["id"],
        "expected_outcome": case["expected_outcome"],
        "checks": [check.as_dict() for check in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", default="/job.json")
    args = parser.parse_args(argv)
    result = verify(json.loads(Path(args.job).read_text(encoding="utf-8")))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
