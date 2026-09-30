"""The bench tier's image on other distributions, as `tools/bench_in_container.py` builds it.

`--distribution` builds the tier's image on a distribution other than the
default image's. The part of the image that is the distribution's, the base
image and the packages OpenOCD, the cross compiler, GDB, CMake and Python come
from, is a head of that distribution's own under tools/bench/distributions.
Everything from tools/bench/Dockerfile's first WORKDIR on is that file's own,
so what the image installs of the checkout, and everything the runner relies
on, is the same on every distribution and changes with the default image.

Nothing here builds an image or needs a board. The runtime is the fake that
records every command, from test_bench_in_container.py beside this file, and a
distribution's image building, and the tier passing in it, are proven by a run
on the machine the board is attached to.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bench_in_container  # noqa: E402
import run_lock  # noqa: E402
from test_bench_in_container import (  # noqa: E402, F401
    COMMIT,
    DOCKERFILE,
    IGNORE_FILE,
    IMAGE_ID,
    a_machine_of_this_tests_own,
    machine,
    option_values,
    run,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HEADS = REPOSITORY_ROOT / "tools" / "bench" / "distributions"

# The distributions a run can name, and the image each head starts from. At
# least the two Ubuntu long-term releases a newcomer is most likely to have,
# the Debian release the default image's base is not, and the current Fedora
# release, which packages all of it under other names.
BASES = {
    "ubuntu-22.04": "ubuntu:22.04",
    "ubuntu-24.04": "ubuntu:24.04",
    "debian-12": "debian:12",
    "fedora-44": "fedora:44",
}

# Every package the default image installs, and what each distribution family
# calls it. A package the default image gains fails the test that reads this
# until every head accounts for it, because a head that lacks it builds an image
# the tier fails in for a reason that has nothing to do with the distribution.
# Fedora packages the C++ compiler apart from the C compiler, and the demo's
# CMake project enables C++; it packages no GDB for ARM of its own, and its
# `gdb` is the one the product finds.
EQUIVALENTS = {
    "openocd": {"apt": {"openocd"}, "dnf": {"openocd"}},
    "gcc-arm-none-eabi": {"apt": {"gcc-arm-none-eabi"}, "dnf": {"arm-none-eabi-gcc-cs", "arm-none-eabi-gcc-cs-c++"}},
    "libnewlib-arm-none-eabi": {"apt": {"libnewlib-arm-none-eabi"}, "dnf": {"arm-none-eabi-newlib"}},
    "gdb-multiarch": {"apt": {"gdb-multiarch"}, "dnf": {"gdb"}},
    "cmake": {"apt": {"cmake"}, "dnf": {"cmake"}},
    "ninja-build": {"apt": {"ninja-build"}, "dnf": {"ninja-build"}},
    "libusb-1.0-0": {"apt": {"libusb-1.0-0"}, "dnf": {"libusb1"}},
    "tini": {"apt": {"tini"}, "dnf": {"tini"}},
}

# What the default image's base carries and a distribution's has to install:
# the interpreter, the virtual environment the shared part creates, and the
# `python` command it creates it with.
PYTHON = {"apt": {"python3", "python3-venv", "python-is-python3"}, "dnf": {"python3", "python-unversioned-command"}}

DIGEST = re.compile(r"^FROM docker\.io/library/(?P<image>[a-z0-9.-]+)@sha256:[0-9a-f]{64}(?: AS (?P<stage>[a-z0-9-]+))?$")


def head_text(distribution: str) -> str:
    return (HEADS / f"{distribution}.Dockerfile").read_text(encoding="utf-8")


def instructions(text: str) -> list[str]:
    """Each instruction on one line, continuations joined and comments dropped."""
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    return joined


def family(head: str) -> str:
    installs = [line for line in instructions(head) if line.startswith("RUN ")]
    if any("apt-get install" in line for line in installs):
        return "apt"
    if any("dnf install" in line for line in installs):
        return "dnf"
    raise AssertionError(f"the head installs with neither apt-get nor dnf: {installs}")


def installed_packages(head: str) -> set[str]:
    """The package names the head's one install line names, options left out."""
    installs = [
        line for line in instructions(head) if line.startswith("RUN ") and re.search(r"\b(apt-get|dnf) install ", line)
    ]
    assert len(installs) == 1, installs
    command = next(part for part in installs[0].split("&&") if re.search(r"\b(apt-get|dnf) install ", part))
    return {word for word in command.split(" install ", 1)[1].split() if not word.startswith("-")}


def default_packages() -> set[str]:
    """What the default file's own distribution part installs, before its first WORKDIR."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("WORKDIR "))
    return installed_packages("\n".join(lines[:start]))


def first_stage() -> str:
    """The name the default file gives its first stage, which its later stages build on."""
    line = next(line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("FROM "))
    words = line.split()
    assert len(words) == 4 and words[2] == "AS", line
    return words[3]


def shared_part() -> str:
    """What every image shares: the default file from its first WORKDIR on."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("WORKDIR "))
    return "\n".join(lines[start:]) + "\n"


# The heads.


def test_the_runner_offers_every_head_and_nothing_else() -> None:
    offered = set(bench_in_container.DISTRIBUTIONS)

    assert offered == set(BASES), offered
    assert {path.stem for path in HEADS.glob("*.Dockerfile")} == offered


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_starts_from_its_distribution_pinned_by_digest(distribution: str) -> None:
    """Pinned the way the default image pins its own base, with the tag it was
    resolved from on the line beneath, and named with its registry for Podman."""
    lines = head_text(distribution).splitlines()
    froms = [index for index, line in enumerate(lines) if line.startswith("FROM ")]

    assert len(froms) == 1, froms
    match = DIGEST.match(lines[froms[0]])
    assert match, lines[froms[0]]
    image, tag = BASES[distribution].split(":")
    assert match["image"] == image
    assert lines[froms[0] + 1] == f"# ^ {image}:{tag}"


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_names_its_stage_as_the_default_file_names_the_one_its_later_stages_build_on(
    distribution: str,
) -> None:
    """The default file's later stages start FROM its first stage by name, and
    under a head that named its stage otherwise a build would look for an image
    of that name in a registry instead."""
    (line,) = [line for line in head_text(distribution).splitlines() if line.startswith("FROM ")]

    assert DIGEST.match(line)["stage"] == first_stage(), line


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_is_the_distributions_part_and_nothing_of_the_shared_one(distribution: str) -> None:
    """No working directory, no install of the checkout, no marker and no entry
    point: all of that is the default file's, and a head that set any of it
    would be an image whose runner contract is not the one the tests hold."""
    words = {line.split()[0] for line in instructions(head_text(distribution))}

    assert words <= {"FROM", "RUN"}, words
    assert "pip" not in head_text(distribution)


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_installs_what_the_default_image_installs_under_its_own_names(distribution: str) -> None:
    head = head_text(distribution)
    kind = family(head)
    packages = installed_packages(head)

    for default, names in EQUIVALENTS.items():
        assert names[kind] <= packages, (
            f"{distribution} installs nothing for the default image's {default}: {sorted(packages)}"
        )
    assert PYTHON[kind] <= packages, sorted(packages)


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_installs_without_recommendations(distribution: str) -> None:
    """The default image's rule: nothing the tier does not use, so nothing the
    results could come to depend on."""
    install = next(line for line in instructions(head_text(distribution)) if " install " in line)

    assert "--no-install-recommends" in install or "install_weak_deps=False" in install, install


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_each_head_names_its_distribution_where_the_bench_tier_reads_it(distribution: str) -> None:
    """The tier leaves a stage out on a distribution that cannot run it, under
    the name the image gives, and never on an image that names none. A head
    that named nothing would have its image held to every stage the default
    image runs."""
    from tests.bench.conftest import DISTRIBUTION_NAME

    named = [line for line in instructions(head_text(distribution)) if DISTRIBUTION_NAME.as_posix() in line]

    assert named == [
        f"RUN mkdir -p {DISTRIBUTION_NAME.parent.as_posix()} && printf '%s\\n' {distribution} > {DISTRIBUTION_NAME.as_posix()}"
    ], named


def test_the_default_image_names_no_distribution() -> None:
    """An image that names none is held to every stage, which is what keeps the
    image the gate runs in from losing one without failing it. The shared part
    is in every distribution's image too, so it names none either."""
    from tests.bench.conftest import DISTRIBUTION_NAME

    assert DISTRIBUTION_NAME.as_posix() not in DOCKERFILE.read_text(encoding="utf-8")


def test_every_package_of_the_default_image_is_accounted_for() -> None:
    """A package added to the default image has to be added to every head too."""
    assert default_packages() == set(EQUIVALENTS), sorted(default_packages() ^ set(EQUIVALENTS))


def test_the_shared_part_follows_one_distribution_part() -> None:
    """The composition's premise: one stage with its base and its packages, and
    the rest from the first WORKDIR on. A later stage starts from that first
    stage by name, so it follows whichever distribution the head brought; one
    from a base image of its own would have to be taught to the composition
    before a distribution can share it."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    froms = [index for index, line in enumerate(lines) if line.startswith("FROM ")]
    workdirs = [index for index, line in enumerate(lines) if line.startswith("WORKDIR ")]

    assert workdirs and froms[0] < workdirs[0], (froms, workdirs)
    assert all(index > workdirs[0] and lines[index].split()[1] == first_stage() for index in froms[1:]), [
        lines[index] for index in froms
    ]


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_a_composed_file_is_the_head_then_the_default_files_shared_part(distribution: str) -> None:
    head = head_text(distribution)

    composed = bench_in_container.compose_dockerfile(DOCKERFILE.read_text(encoding="utf-8"), head)

    assert composed.startswith(head.rstrip("\n") + "\n")
    assert composed.endswith(shared_part())
    froms = [line for line in composed.splitlines() if line.startswith("FROM ")]
    assert froms[:1] == [line for line in head.splitlines() if line.startswith("FROM ")]
    assert all(line.split()[1] == first_stage() for line in froms[1:]), froms


def test_a_default_file_without_a_shared_part_is_refused() -> None:
    with pytest.raises(ValueError, match="WORKDIR"):
        bench_in_container.compose_dockerfile("FROM scratch\nRUN true\n", head_text("debian-12"))


# The runner.


def a_tree_with_heads(root: Path, commit: str, destination: Path) -> None:
    """The committed tree, with the default file, its ignore file and the heads."""
    bench = destination / "tools" / "bench"
    (bench / "distributions").mkdir(parents=True)
    (bench / "Dockerfile.dockerignore").write_text(IGNORE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    (bench / "Dockerfile").write_text(
        "FROM docker.io/library/python@sha256:"
        + "0" * 64
        + "\nRUN apt-get install tini\nWORKDIR /work\nLABEL shared=yes\n",
        encoding="utf-8",
    )
    for distribution in BASES:
        (bench / "distributions" / f"{distribution}.Dockerfile").write_text(
            f"FROM docker.io/library/{distribution}@sha256:{'1' * 64}\nRUN install {distribution}\n", encoding="utf-8"
        )


@pytest.fixture
def heads(machine: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """The machine, with a committed tree that carries the heads, and the file each build read."""
    monkeypatch.setattr(bench_in_container, "stage_committed_tree", a_tree_with_heads)
    built: list[tuple[str, str]] = []
    original = machine.runtime.popen
    # The machine's fixture handed the tool the fake's own method, so the
    # recording goes where the tool looks for it.

    def popen(command: list[str], **kwargs: object):
        if machine.runtime.verb(command) == "build":
            file = command[command.index("--file") + 1]
            built.append((file, Path(file).read_text(encoding="utf-8")))
        return original(command, **kwargs)

    monkeypatch.setattr(bench_in_container.subprocess, "Popen", popen)
    machine.built = built
    return machine


@pytest.mark.parametrize("distribution", sorted(BASES))
def test_a_distribution_builds_its_head_with_the_shared_part(heads: SimpleNamespace, distribution: str) -> None:
    """The file the build reads is composed apart from the committed tree, which
    stays exactly what was committed, and the image is tagged with the
    distribution so each one's build keeps its own."""
    assert run(heads, "--distribution", distribution) == 0

    build = heads.runtime.issued("build")[0]
    context = Path(build[-1])
    ((file, text),) = heads.built
    assert not Path(file).is_relative_to(context), (file, context)
    assert (
        text
        == f"FROM docker.io/library/{distribution}@sha256:{'1' * 64}\nRUN install {distribution}\n\nWORKDIR /work\nLABEL shared=yes\n"
    )
    assert option_values(build, "--tag") == [f"{bench_in_container.IMAGE}:{distribution}"]
    assert option_values(build, "--label") == [f"org.opencontainers.image.revision={COMMIT}"]
    tier = heads.runtime.tier
    assert tier[tier.index(bench_in_container.CONTAINER_SCRIPT) - 3] == IMAGE_ID


def test_without_a_distribution_the_default_file_is_built_as_it_stands(heads: SimpleNamespace) -> None:
    assert run(heads) == 0

    build = heads.runtime.issued("build")[0]
    context = Path(build[-1])
    assert heads.built[0][0] == str(context / "tools" / "bench" / "Dockerfile")
    assert option_values(build, "--tag") == [bench_in_container.IMAGE]


def test_a_distribution_the_runner_does_not_offer_is_refused_before_anything_runs(
    heads: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as exited:
        run(heads, "--distribution", "arch")

    assert exited.value.code == 2
    assert "ubuntu-22.04" in capsys.readouterr().err
    assert heads.runtime.commands == []


def test_a_commit_without_the_head_is_refused_naming_it(
    heads: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A commit from before a distribution was added has no head for it, and
    building the default image under its name would be a wrong answer."""

    def a_tree_without_heads(root: Path, commit: str, destination: Path) -> None:
        a_tree_with_heads(root, commit, destination)
        (destination / "tools" / "bench" / "distributions" / "fedora-44.Dockerfile").unlink()

    monkeypatch.setattr(bench_in_container, "stage_committed_tree", a_tree_without_heads)

    assert run(heads, "--distribution", "fedora-44") == bench_in_container.EXIT_BUILD_FAILED

    assert "tools/bench/distributions/fedora-44.Dockerfile" in capsys.readouterr().err
    assert heads.runtime.issued("build") == []
    assert heads.runtime.issued("run") == []
    assert not run_lock.lock_path().exists()


def test_the_machine_is_held_in_the_name_of_the_distributions_image(heads: SimpleNamespace) -> None:
    """Whoever queues behind the run is told which image is on the board."""
    seen: list[dict] = []
    heads.runtime.during_run = lambda command: seen.append(json.loads(run_lock.lock_path().read_text(encoding="utf-8")))

    assert run(heads, "--distribution", "ubuntu-22.04") == 0

    assert seen[0]["image"] == f"{bench_in_container.IMAGE}:ubuntu-22.04"
    assert seen[0]["distribution"] == "ubuntu-22.04"


def test_the_build_says_which_distribution_it_builds(heads: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    assert run(heads, "--distribution", "debian-12") == 0

    assert "on debian-12" in capsys.readouterr().err


def test_the_image_alone_is_built_on_a_distribution(heads: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_in_container, "this_is_linux", lambda: False)

    assert run(heads, "--build-only", "--distribution", "fedora-44") == 0

    assert option_values(heads.runtime.issued("build")[0], "--tag") == [f"{bench_in_container.IMAGE}:fedora-44"]
    assert heads.runtime.issued("run") == []
