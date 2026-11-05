"""Report build space and retire one nightly bench image under the run lock."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_in_container import DISTRIBUTIONS, IMAGE_LABEL, image_name  # noqa: E402
from run_lock import RunLock, RunLockBusy, withheld_voice  # noqa: E402


def report() -> None:
    store = subprocess.run(
        ["podman", "info", "--format", "{{.Store.GraphRoot}}"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout.strip()
    # Both places can fill while a layer is committed. Their paths can name
    # the runner's account, so only their roles and free GiB reach the log.
    for role, place in (("Image store", store), ("Layer temporary storage", os.environ.get("TMPDIR") or "/var/tmp")):
        print(f"{role}: {shutil.disk_usage(place).free / 1024**3:.1f} GiB free", flush=True)
    subprocess.run(["podman", "system", "df"], check=True, timeout=120)


def remove(distribution: str) -> None:
    # A failed removal must still allow pruning and reporting. No image is
    # forced away from a container that is using it, and base images stay.
    commands = [
        "podman ps --all --external --no-trunc --format '{{if eq .State \"storage\"}}{{.ID}}{{end}}' | xargs --no-run-if-empty podman rm --force",
        f"podman image ls --quiet --filter {shlex.quote(f'label={IMAGE_LABEL}')} --filter {shlex.quote(f'reference={image_name(distribution)}')} | xargs --no-run-if-empty podman image rm",
        "podman image prune --force",
    ]
    for command in commands:
        try:
            subprocess.run(["bash", "-o", "pipefail", "-c", command], check=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"::warning::Image cleanup failed ({type(error).__name__}); later distributions still run.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before", "after"))
    parser.add_argument("--distribution", choices=DISTRIBUTIONS, required=True)
    options = parser.parse_args(argv)
    lock = RunLock(tool="tools/bench_image_store.py", runs_for="a minute or two", announce=withheld_voice())
    try:
        print(f"{options.phase} {options.distribution}", flush=True)
        # Only removal changes the store. A report ahead of a build reads it,
        # and waiting behind a run on the machine would spend its five minutes
        # in the queue the build itself joins next.
        if options.phase == "after":
            lock.acquire()
            remove(options.distribution)
        report()
    except (OSError, subprocess.SubprocessError, RunLockBusy) as error:
        print(f"::warning::Image store operation failed ({type(error).__name__}).", flush=True)
        return 1
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
