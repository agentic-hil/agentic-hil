"""Gate real install.sh downloads and offline registration of the current wheel.

Usage: python tools/test_agent_registration.py [--output DIRECTORY]
Docker/build/startup failures, timeouts, absent reports and incomplete matrices
all fail. No credentials or host directories are mounted into the container.
"""

from __future__ import annotations

import argparse
import hashlib
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

from evals.install.registration_gate import REPORT_PREFIX, SCRIPT_SCENARIOS, validate_report  # noqa: E402


def read_report(output: str, *, mode: str = "all") -> dict:
    lines = [line.removeprefix(REPORT_PREFIX) for line in output.splitlines() if line.startswith(REPORT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("container must emit exactly one registration gate report")
    report = json.loads(lines[0])
    validate_report(report, mode=mode)
    return report


def combine_reports(reports: list[dict], installer_sha256: str) -> dict:
    if len(reports) != 2 or {report["mode"] for report in reports} != {"script", "wheel"}:
        raise ValueError("both script and wheel registration stages must pass")
    for report in reports:
        validate_report(report, mode=report["mode"])
    if reports[0]["versions"] != reports[1]["versions"]:
        raise ValueError("registration stages used different agent CLI versions")
    report = {
        "ok": True, "mode": "all", "versions": reports[0]["versions"],
        "cases": [row for stage in reports for row in stage["cases"]],
        "image_ids": {stage["mode"]: stage["image_id"] for stage in reports},
    }
    for row in report["cases"]:
        if row["scenario"] in SCRIPT_SCENARIOS and row.get("installer_sha256") != installer_sha256:
            raise ValueError("script stage did not execute this checkout's install.sh")
    validate_report(report)
    return report


def container_command(docker: str, name: str, image_id: str, mode: str) -> list[str]:
    if mode not in {"script", "wheel"}:
        raise ValueError(f"unknown registration mode: {mode}")
    return [
        docker, "run", "--rm", "--name", name,
        "--network", "bridge" if mode == "script" else "none",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", image_id,
    ]


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
            reports = []
            installer_sha256 = hashlib.sha256((ROOT / "install.sh").read_bytes()).hexdigest()
            for mode, target in (("script", "script-registration-gate"), ("wheel", "registration-gate")):
                iidfile = Path(temporary) / f"{mode}-image-id"
                print(f"Building the {mode} registration image and locked agent CLIs...", flush=True)
                run_logged([
                    docker, "build", "--target", target, "--iidfile", str(iidfile),
                    "--file", "evals/install/container/Dockerfile", ".",
                ], args.output / f"{mode}-build.log", 900)
                image_id = iidfile.read_text().strip()
                if not image_id.startswith("sha256:"):
                    raise RuntimeError("Docker did not produce an immutable image id")
                print(f"Running {mode} registration cases ({'downloads enabled' if mode == 'script' else 'offline'})...", flush=True)
                log_path = args.output / f"{mode}-container.log"
                run_logged(container_command(docker, name, image_id, mode), log_path, 900)
                stage = read_report(log_path.read_text(encoding="utf-8"), mode=mode)
                stage["image_id"] = image_id
                reports.append(stage)
            report = combine_reports(reports, installer_sha256)
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
