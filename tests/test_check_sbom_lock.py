"""Exercise the SBOM lock check with artifacts generated locally for pip."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import platform
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from textwrap import dedent

import pytest

from tools import check_sbom_lock as checker

ROOT = Path(__file__).resolve().parents[1]

# pip can prepare this project's metadata in isolation using only the standard
# library. A wheel should never be built during the download probe.
SOURCE_BACKEND = dedent('''\
    import zipfile
    from pathlib import Path


    def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
        directory = Path(metadata_directory) / DIST_INFO
        directory.mkdir()
        for filename in ("METADATA", "WHEEL"):
            (directory / filename).write_bytes(WHEEL_FILES[f"{DIST_INFO}/{filename}"])
        return directory.name


    def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
        with zipfile.ZipFile(Path(wheel_directory) / WHEEL_NAME, "w") as archive:
            for filename, content in WHEEL_FILES.items():
                archive.writestr(filename, content)
        return WHEEL_NAME
    ''')


def _metadata(name: str, version: str) -> str:
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"


def _dist_info(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}.dist-info"


def _wheel_files(name: str, version: str, tag: str) -> dict[str, bytes]:
    dist_info = _dist_info(name, version)
    record_path = f"{dist_info}/RECORD"
    files = {
        f"{dist_info}/METADATA": _metadata(name, version).encode(),
        f"{dist_info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n".encode(),
    }
    records = []
    for filename, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
        records.append(f"{filename},sha256={digest},{len(data)}\n")
    files[record_path] = ("".join(records) + f"{record_path},,\n").encode()
    return files


def _wheel(directory: Path, name: str, version: str, tag: str = "py3-none-any") -> Path:
    path = directory / f"{name.replace('-', '_')}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in _wheel_files(name, version, tag).items():
            archive.writestr(filename, content)
    return path


def _sdist(directory: Path, name: str, version: str) -> Path:
    stem = f"{name.replace('-', '_')}-{version}"
    path = directory / f"{stem}.tar.gz"
    metadata = _metadata(name, version)
    files = {
        "PKG-INFO": metadata,
        "pyproject.toml": dedent('''\
            [build-system]
            requires = []
            build-backend = "backend"
            backend-path = ["."]
            '''),
        "backend.py": (
            f"DIST_INFO = {_dist_info(name, version)!r}\n"
            f"WHEEL_NAME = {stem + '-py3-none-any.whl'!r}\n"
            f"WHEEL_FILES = {_wheel_files(name, version, 'py3-none-any')!r}\n\n{SOURCE_BACKEND}"
        ),
    }
    with tarfile.open(path, "w:gz") as archive:
        for filename, content in files.items():
            data = content.encode("utf-8")
            member = tarfile.TarInfo(f"{stem}/{filename}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def _pin(requirement: str, *artifacts: Path) -> str:
    hashes = [f"--hash=sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}" for path in artifacts]
    return " \\\n    ".join([requirement, *hashes]) + "\n"


@pytest.fixture
def offline_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if importlib.util.find_spec("pip") is None:
        pytest.skip("The running Python has no pip for the offline download check")

    index = tmp_path / "index"
    index.mkdir()
    for key in tuple(os.environ):
        if key.upper().startswith("PIP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", index.as_uri())
    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    return index


def test_split_passes_and_folded_pin_reports_requirement_python_and_remedy(
    tmp_path: Path, offline_index: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    old = "rpds-py==0.30.0"
    old_source = _sdist(offline_index, "rpds-py", "0.30.0")
    old_wheel = _wheel(offline_index, "rpds-py", "0.30.0", "py2-none-any")
    new_wheel = _wheel(offline_index, "rpds-py", "2026.9.1")

    # The release selects the newer half. Exercise that selection even on the
    # 3.10 matrix leg, which would choose the older pin with the real boundary.
    boundary = "3.10" if sys.version_info[:2] == (3, 10) else "3.11"
    lock = tmp_path / "requirements" / "sbom.txt"
    lock.parent.mkdir()
    lock.write_text(
        _pin(f"{old} ; python_full_version < '{boundary}'", old_source, old_wheel)
        + _pin(f"rpds-py==2026.9.1 ; python_full_version >= '{boundary}'", new_wheel),
        encoding="utf-8",
    )
    assert checker.check_lock(lock, {}) == 0
    capfd.readouterr()

    lock.write_text(_pin(old, old_source, old_wheel), encoding="utf-8")
    assert checker.check_lock(lock, {}) != 0
    diagnostics = capfd.readouterr().err.splitlines()
    assert any(line.startswith("ERROR: ") and old in line for line in diagnostics[:-1]), diagnostics
    final_line = diagnostics[-1].replace("\\", "/")
    assert "requirements/sbom.txt" in final_line
    assert f"Python {platform.python_version()}" in final_line
    remedy = final_line.lower()
    assert "restore a pin" in remedy and "wheel" in remedy
    assert "add" in remedy and "reason" in remedy and "allowed source builds" in remedy


def test_compatible_wheel_with_wrong_hash_fails(
    tmp_path: Path, offline_index: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    _wheel(offline_index, "rpds-py", "2026.9.1")
    lock = tmp_path / "sbom.txt"
    lock.write_text(f"rpds-py==2026.9.1 --hash=sha256:{'0' * 64}\n", encoding="utf-8")
    assert checker.check_lock(lock, {}) != 0
    assert "do not match the hashes" in capfd.readouterr().err.lower()


def test_source_only_pin_requires_a_reasoned_exception(tmp_path: Path, offline_index: Path) -> None:
    name = "source-only"
    source = _sdist(offline_index, name, "1.0")
    lock = tmp_path / "sbom.txt"
    lock.write_text(_pin(f"{name}==1.0", source), encoding="utf-8")
    assert checker.check_lock(lock, {}) != 0
    assert checker.check_lock(lock, {name: "The project publishes an sdist only."}) == 0


def test_blank_source_build_reason_is_refused_before_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_process(*args: object, **kwargs: object) -> None:
        pytest.fail("The checker tried to start pip with a blank source-build reason")

    lock = tmp_path / "sbom.txt"
    lock.write_text("source-only==1.0\n", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", refuse_process)
        assert checker.check_lock(lock, {"source-only": " \t"}) != 0


def test_sbom_checker_test_is_excluded_from_the_source_distribution() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    exclusion = "exclude tests/test_check_sbom_lock.py"
    if exclusion not in manifest:
        raise AssertionError("The source distribution includes the repository-only SBOM checker test")
    image_exclusion = manifest.index("exclude tests/test_check_image_pip_locks.py")
    assert abs(manifest.index(exclusion) - image_exclusion) == 1
