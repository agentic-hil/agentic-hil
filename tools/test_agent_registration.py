"""Build and run mandatory offline registration gates, including uncommitted work.

Usage: python tools/test_agent_registration.py [--output DIRECTORY]
Docker/build/startup failures, timeouts, absent reports and incomplete matrices
all fail. No credentials or host directories are mounted into the container.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.install.registration_gate import REPORT_PREFIX, validate_report  # noqa: E402


def read_report(output: str) -> dict:
    lines = [line.removeprefix(REPORT_PREFIX) for line in output.splitlines() if line.startswith(REPORT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("container must emit exactly one registration gate report")
    report = json.loads(lines[0])
    validate_report(report)
    return report


def run_logged(command: list[str], path: Path, timeout: int) -> subprocess.CompletedProcess:
    with path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{path.read_text(encoding='utf-8')[-12000:]}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/install/artifacts/registration-gate")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    # Never leave a previous passing report beside a new failed build.
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps({"ok": False, "error": "run has not completed"}) + "\n", encoding="utf-8")
    docker = shutil.which("docker")
    if docker is None:
        print("Docker is required; registration gates cannot be skipped.", file=sys.stderr)
        return 1
    name = "agentic-hil-registration-" + uuid.uuid4().hex
    try:
        info = subprocess.run([docker, "info", "--format", "{{.OSType}}"], capture_output=True, text=True, timeout=30, check=True)
        if info.stdout.strip() != "linux":
            raise RuntimeError("registration gates require a Linux Docker engine")
        with tempfile.TemporaryDirectory(prefix="agentic-hil-registration-") as temporary:
            iidfile = Path(temporary) / "image-id"
            print("Building the current working tree and locked agent CLIs...", flush=True)
            run_logged([
                docker, "build", "--target", "registration-gate", "--iidfile", str(iidfile),
                "--file", "evals/install/container/Dockerfile", ".",
            ], args.output / "build.log", 900)
            image_id = iidfile.read_text().strip()
            if not image_id.startswith("sha256:"):
                raise RuntimeError("Docker did not produce an immutable image id")
            print(f"Running all registration cases offline as an unprivileged user ({image_id})...", flush=True)
            run_logged([
                docker, "run", "--rm", "--name", name, "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "256", image_id,
            ], args.output / "container.log", 900)
            report = read_report((args.output / "container.log").read_text(encoding="utf-8"))
            report["image_id"] = image_id
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"PASS: {len(report['cases'])} registration cases; {report['versions']}; report: {report_path}")
        return 0
    except (OSError, ValueError, KeyError, AssertionError, RuntimeError, subprocess.SubprocessError) as error:
        report_path.write_text(json.dumps({"ok": False, "error": str(error)}, indent=2) + "\n", encoding="utf-8")
        print(f"Registration gate FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        # Only our uniquely named container; this also cleans up after timeout.
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run([docker, "rm", "--force", name], capture_output=True, timeout=30, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
