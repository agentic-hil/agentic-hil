"""Check the hashed locks with each image's own pip, including its bootstrap."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_in_container import DISTRIBUTIONS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ("default", *DISTRIBUTIONS)
LOCKS = ("container", "dev", "runtime", "production")
PYTHON = "/work/.venv/bin/python"


def image_base(image: str) -> tuple[str, str]:
    head = ROOT / "tools/bench" / ("Dockerfile" if image == "default" else f"distributions/{image}.Dockerfile")
    text = head.read_text(encoding="utf-8")
    base = re.search(r"^FROM (\S+@sha256:[0-9a-f]{64})", text, re.MULTILINE)
    if base is None:
        raise ValueError("The image has no digest-pinned base.")
    if image == "default":
        return base[1], ""
    install = next(
        (line for line in text.replace("\\\n", " ").splitlines() if re.search(r"\b(?:apt-get|dnf) install ", line)),
        None,
    )
    if install is None:
        raise ValueError("The image head has no package installation command.")
    packages = [word for word in shlex.split(install.split(" install ", 1)[1].split("&&", 1)[0]) if word.startswith("python")]
    if not packages:
        raise ValueError("The image head names no Python packages.")
    if "apt-get install " in install:
        setup = "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes "
    else:
        setup = "dnf install --assumeyes --setopt=install_weak_deps=False "
    return base[1], setup + shlex.join(packages)


def check_image(image: str, runtime: str) -> int:
    name = f"agentic-hil-pip-locks-{uuid.uuid4().hex}"
    pip_version = "unavailable before the virtual environment is created"
    lock = "requirements/container.txt"
    status = 0

    def run(arguments: list[str], *, capture: bool = False) -> str:
        result = subprocess.run(
            [runtime, *arguments], check=True, text=True, capture_output=capture, timeout=900,
        )
        return result.stdout.strip() if capture else ""

    try:
        base, setup = image_base(image)
        run(["run", "--detach", "--name", name, "--volume", f"{ROOT}:/checkout:ro", base, "sleep", "infinity"], capture=True)
        if setup:
            run(["exec", name, "sh", "-c", setup])
        run(["exec", name, "python", "-m", "venv", "/work/.venv"])
        pip_version = run(["exec", name, PYTHON, "-m", "pip", "--version"], capture=True)
        print(f"{image}: {pip_version}", flush=True)
        for entry in LOCKS:
            lock = f"requirements/{entry}.txt"
            arguments = ["exec", name, PYTHON, "-m", "pip", "install", "--disable-pip-version-check"]
            # Older image pips have no dry-run option. The real first install
            # replaces pip before it is asked to resolve the other locks.
            if entry != "container":
                arguments += ["--dry-run", "--ignore-installed"]
            run([*arguments, "--require-hashes", "-r", f"/checkout/{lock}"])
            if entry == "container":
                pip_version = run(["exec", name, PYTHON, "-m", "pip", "--version"], capture=True)
                print(f"{image}: after {lock}: {pip_version}", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"image={image} lock={lock} pip={pip_version}: check failed ({type(error).__name__}).", file=sys.stderr)
        status = 1
    finally:
        try:
            run(["rm", "--force", name], capture=True)
        except (OSError, subprocess.SubprocessError):
            print(f"image={image} lock={lock} pip={pip_version}: container cleanup failed.", file=sys.stderr)
            status = 1
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", choices=IMAGES, default="default")
    parser.add_argument("--runtime", choices=("docker", "podman"), default="docker")
    options = parser.parse_args(argv)
    return check_image(options.image, options.runtime)


if __name__ == "__main__":
    sys.exit(main())
