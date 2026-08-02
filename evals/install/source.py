from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    # A redirected APPDATA/LOCALAPPDATA for a local test run. It is git-ignored
    # machine-local environment, never source, and it holds live lock files that
    # a digest of the tree has no business reading.
    ".testenv",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "dist",
}
# A normal ``pip install <directory>`` lets the build backend write
# ``<package>.egg-info`` into the tree it builds from. That is expected install
# output, not a modified source snapshot.
IGNORED_DIRECTORY_SUFFIXES = (".egg-info",)
IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}
SNAPSHOT_ROOT_FILES = (
    "AI_AGENT_QUICKSTART.md",
    "LICENSE",
    "LICENSE.txt",
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
)


def require_symlink_free_tree(root: Path) -> Path:
    """Return an absolute tree root after rejecting links and special files."""
    root = Path(os.path.abspath(root))
    try:
        root_info = root.lstat()
    except OSError as error:
        raise ValueError(f"source tree cannot be inspected: {root}: {error}") from error
    if stat.S_ISLNK(root_info.st_mode):
        raise ValueError(f"source tree root may not be a symlink: {root}")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"source tree root is not a directory: {root}")

    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in sorted(directory_names):
            candidate = parent / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"source tree may not contain symlinks: {candidate}")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"source tree contains non-directory entry: {candidate}")
        for name in sorted(file_names):
            candidate = parent / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"source tree may not contain symlinks: {candidate}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"source tree contains non-regular file: {candidate}")
    return root


def source_files(root: Path) -> list[Path]:
    root = Path(os.path.abspath(root))
    try:
        root_info = root.lstat()
    except OSError as error:
        raise ValueError(f"source tree cannot be inspected: {root}: {error}") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"source tree root must be a non-symlink directory: {root}")

    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = parent / name
            if name in IGNORED_DIRECTORY_NAMES or name.endswith(IGNORED_DIRECTORY_SUFFIXES):
                continue
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"source tree may not contain symlinks: {candidate}")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"source tree contains non-directory entry: {candidate}")
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            candidate = parent / name
            if candidate.suffix in IGNORED_FILE_SUFFIXES:
                continue
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"source tree may not contain symlinks: {candidate}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"source tree contains non-regular file: {candidate}")
            files.append(candidate)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def source_digest(root: Path) -> str:
    root = Path(os.path.abspath(root))
    digest = hashlib.sha256()
    for path in source_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def create_source_snapshot(source_root: Path, destination: Path) -> Path:
    source_root = source_root.resolve()
    required = ("AI_AGENT_QUICKSTART.md", "README.md", "pyproject.toml")
    missing = [name for name in required if not (source_root / name).is_file()]
    package_root = source_root / "src" / "agentic_hil"
    if not package_root.is_dir():
        missing.append("src/agentic_hil")
    if missing:
        raise ValueError(f"source snapshot is missing required paths: {missing}")
    package_root = require_symlink_free_tree(package_root)
    for name in SNAPSHOT_ROOT_FILES:
        source = source_root / name
        if source.is_symlink():
            raise ValueError(f"source snapshot path must be a regular file: {source}")
    if destination.exists():
        raise FileExistsError(f"source snapshot destination already exists: {destination}")

    destination.mkdir(parents=True)
    for name in SNAPSHOT_ROOT_FILES:
        source = source_root / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"source snapshot path must be a regular file: {source}")
        shutil.copy2(source, destination / name)

    destination_package = destination / "src" / "agentic_hil"
    for source in source_files(package_root):
        if source.is_symlink():
            raise ValueError(f"source snapshot may not contain symlinks: {source}")
        relative = source.relative_to(package_root)
        target = destination_package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination
