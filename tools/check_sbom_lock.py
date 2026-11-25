"""Check the release interpreter's SBOM lock."""

import platform
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

ALLOWED_SOURCE_BUILDS: dict[str, str] = {}


def check_lock(lock_path: Path, allowed_source_builds: Mapping[str, str]) -> int:
    for package, reason in allowed_source_builds.items():
        if not reason or reason.isspace():
            print(f"Add a reason for {package} in the allowed source builds.", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory() as destination:
        command = [
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--require-hashes", "--only-binary=:all:", "--dest", destination,
            "-r", str(lock_path),
        ]
        if allowed_source_builds:
            command.extend(["--no-binary", ",".join(allowed_source_builds)])
        result = subprocess.run(command, check=False)

    if result.returncode:
        print(
            f"{lock_path.as_posix()} failed on Python {platform.python_version()}. "
            "Restore a pin with a wheel for this Python, or add the package and a reason to the allowed source builds.",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(check_lock(Path("requirements/sbom.txt"), ALLOWED_SOURCE_BUILDS))
