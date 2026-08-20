"""The TLS-proxy bench eval: what it must be, and what running it must prove.

`evals/tls_proxy/` is one Docker image that is a Linux bench behind a
TLS-inspecting proxy: mitmproxy on loopback, its CA in the machine's own trust
store, `HTTPS_PROXY` exported, uv installed. On such a bench `agentic-hil
upgrade` fails with `invalid peer certificate: UnknownIssuer` while curl and apt
keep working, because uv validates against roots compiled into its own binary.

Two kinds of test live here. The static ones read the committed files and
hold the properties that make the reproduction worth anything: the base image is
pinned, the machine trusts the proxy through the system store rather than
through a switch, the proof runs without the variable the seed needs, and
neither file ever turns certificate verification off. They cost nothing and run
everywhere.

The container run is the eval itself. It needs Docker, the network and several
minutes, so it is opt-in twice over: Docker has to be here and answering for
Linux containers, and `AGENTIC_HIL_TLS_PROXY_EVAL` has to be set on purpose.
Default CI sets neither, which is deliberate: this asks the live index and the
live release for an answer, and a unit test run is not the place to spend that.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIRECTORY = REPOSITORY_ROOT / "evals" / "tls_proxy"
DOCKERFILE = EVAL_DIRECTORY / "container" / "Dockerfile"
ENTRYPOINT = EVAL_DIRECTORY / "container" / "entrypoint.sh"
README = EVAL_DIRECTORY / "README.md"

# The one variable that opts a machine in, named in every skip reason below so a
# reader who wanted the eval to run learns how from the run that did not.
OPT_IN_VARIABLE = "AGENTIC_HIL_TLS_PROXY_EVAL"
IMAGE = os.environ.get("AGENTIC_HIL_TLS_PROXY_EVAL_IMAGE", "agentic-hil-tls-proxy-eval:local")

SCRIPT_TIMEOUT_S = 180
DOCKER_PROBE_TIMEOUT_S = 10
# The build pulls a base image and Astral's uv; the run installs two releases of
# this package from the live index through a proxy that re-encrypts every byte.
BUILD_TIMEOUT_S = 1800
CONTAINER_TIMEOUT_S = 1800

# Every spelling of "stop checking the certificate" that the tools in this image
# understand. None of them may appear in either file: the eval's whole claim is
# that the way through a TLS-inspecting proxy keeps verification on, and a run
# that reached the index with verification off would prove the opposite while
# printing the same PASS line.
VERIFICATION_ESCAPES = (
    "--insecure",
    "--no-check-certificate",
    "--allow-insecure-host",
    "--trusted-host",
    "-SkipCertificateCheck",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PYTHONHTTPSVERIFY",
    "UV_INSECURE_HOST",
    "verify=False",
)


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def _both_files() -> dict[str, str]:
    return {DOCKERFILE.name: _dockerfile(), ENTRYPOINT.name: _entrypoint()}


def _code_only(source: str) -> str:
    """The source with whole-line comments dropped.

    A property about what the eval *does* has to be measured on the commands, not
    on the prose beside them: this file's own reasoning names the flags it
    forbids, and so does the Dockerfile's.
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def _posix_shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX sh on this machine; Git Bash provides one on Windows")
    return found


def _docker_and_opt_in() -> str:
    """Docker, if this machine has it and was asked for this eval by name.

    Two gates rather than one. Docker being installed says the run is possible;
    it never says it was wanted. This eval builds an image, talks to PyPI and to
    GitHub through a proxy of its own and takes minutes, so a developer who runs
    the suite gets a skip, and only a deliberate `AGENTIC_HIL_TLS_PROXY_EVAL=1`
    gets the run.
    """
    if not os.environ.get(OPT_IN_VARIABLE):
        pytest.skip(f"the TLS-proxy eval builds and runs a container over the network; set {OPT_IN_VARIABLE}=1 to run it")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip(f"Docker is not installed on this machine, so {OPT_IN_VARIABLE} has nothing to run the eval with")
    try:
        probe = subprocess.run(
            [docker, "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pytest.skip("the Docker daemon did not answer; start Docker and run this again")
    if probe.returncode != 0:
        pytest.skip("the Docker daemon did not answer; start Docker and run this again")
    if probe.stdout.strip() != "linux":
        pytest.skip(f"the Docker daemon runs {probe.stdout.strip() or 'unknown'} containers and {IMAGE} needs linux")
    return docker


def test_the_eval_ships_the_three_files_it_is_documented_as() -> None:
    for path in (DOCKERFILE, ENTRYPOINT, README):
        assert path.is_file(), path


def test_the_base_image_is_pinned_by_digest() -> None:
    """A moving tag makes two runs of this eval incomparable."""
    from_lines = [line for line in _dockerfile().splitlines() if line.startswith("FROM ")]

    assert from_lines, _dockerfile()
    for line in from_lines:
        assert re.search(r"@sha256:[0-9a-f]{64}\b", line), line


def test_the_uv_bootstrap_is_pinned_and_checked_before_it_runs() -> None:
    """The same refusal `install.sh` makes, in the image that installs uv.

    A moving `astral.sh/uv/install.sh` serves whatever is current, so piping it
    into a shell is both an unchecked download and a second moving part beside
    the base image digest. The version and the hash are the ones `install.sh`
    already pins, and they are bumped together.
    """
    dockerfile = _code_only(_dockerfile())

    assert "astral.sh/uv/install.sh" not in dockerfile, dockerfile
    assert re.search(r"astral\.sh/uv/\$\{UV_INSTALLER_VERSION\}/install\.sh", dockerfile), dockerfile
    assert "sha256sum -c -" in dockerfile, dockerfile
    assert re.search(r"ARG UV_INSTALLER_SHA256=[0-9a-f]{64}\b", dockerfile), dockerfile


def test_dependabot_keeps_this_evals_base_image_digest_fresh() -> None:
    """The Dockerfile says a bot keeps its pin fresh; this is what makes that true.

    Dependabot's docker ecosystem reads the Dockerfile in the directory it is
    configured with and nothing below it, so the entry that covers the install
    eval's container says nothing about this one. A digest that never moves is a
    stale image with a security history rather than a safe one, and this eval is
    a bench a reader is invited to build and run themselves.
    """
    configuration = yaml.safe_load((REPOSITORY_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    directories = {entry.get("directory") for entry in configuration["updates"] if entry.get("package-ecosystem") == "docker"}

    assert "/evals/tls_proxy/container" in directories, sorted(str(directory) for directory in directories)


def test_the_machine_trusts_the_proxy_through_its_own_store() -> None:
    """The bench shape in one step: the CA goes where the machine looks.

    Not into a variable a program can be told to read, and not into uv's own
    configuration. `update-ca-certificates` is what makes curl and apt work while
    uv fails, which is the asymmetry the whole eval exists to reproduce.
    """
    dockerfile = _code_only(_dockerfile())

    assert "update-ca-certificates" in dockerfile
    assert "/usr/local/share/ca-certificates/" in dockerfile


def test_the_bench_routes_every_request_through_the_proxy_it_starts() -> None:
    """Every program is pointed at the address the entrypoint listens on.

    The proxy variables are written out literally, so `docker inspect` shows an
    address rather than an expansion, and the listen address is declared once as
    `TLS_PROXY_HOST` and `TLS_PROXY_PORT` for the entrypoint to start mitmdump
    on. Two spellings of one address is exactly the pair that drifts, so they are
    held to each other here rather than trusted to stay in step.
    """
    dockerfile = _code_only(_dockerfile())
    host = re.search(r"TLS_PROXY_HOST=(\S+)", dockerfile)
    port = re.search(r"TLS_PROXY_PORT=(\S+)", dockerfile)

    assert host is not None and port is not None, dockerfile
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{variable}=http://{host.group(1)}:{port.group(1)}" in dockerfile, variable


def test_the_image_never_hands_uv_the_system_store_by_itself() -> None:
    """`UV_SYSTEM_CERTS` in the image environment would answer proof 1 for it.

    The seed sets it for one command, and the released installer sets it for
    itself once it has recognised the failure. An `ENV` line would make both
    meaningless: there would be no failure left to detect.
    """
    assert "UV_SYSTEM_CERTS" not in _code_only(_dockerfile())


def test_the_failing_proof_runs_with_the_variable_removed() -> None:
    """Proof 1 is only a proof if nothing in the environment can have set it."""
    entrypoint = _code_only(_entrypoint())

    assert re.search(r"env -u UV_SYSTEM_CERTS agentic-hil upgrade --json", entrypoint), entrypoint


def test_only_the_seed_reaches_for_the_system_store() -> None:
    """One assignment, on one command, and it is the one that provisions.

    A prefix on a single command, never an export: an export would outlive the
    seed and hand proof 1 the very thing it is measuring the absence of. The
    lines that merely print the seed command back to the reader are not command
    prefixes and are not counted.
    """
    code = _code_only(_entrypoint())
    prefixes = [line.strip() for line in code.splitlines() if re.match(r"\s*(if )?UV_SYSTEM_CERTS=1 ", line)]

    assert "export UV_SYSTEM_CERTS" not in code, code
    assert len(prefixes) == 1, prefixes
    assert prefixes[0].startswith("if UV_SYSTEM_CERTS=1 uv tool install"), prefixes


def test_the_eval_asserts_the_certificate_error_by_its_own_words() -> None:
    """Both halves of the signature the bench reported, matched literally."""
    entrypoint = _entrypoint()

    assert "'invalid peer certificate' 'UnknownIssuer'" in entrypoint, entrypoint
    assert "upgrade_failed" in entrypoint, entrypoint


def test_the_eval_runs_the_released_installer_rather_than_this_checkout() -> None:
    """Proof 2 measures the anchor operators actually get, not the working tree."""
    entrypoint = _code_only(_entrypoint())

    assert "https://github.com/agentic-hil/agentic-hil/releases/latest" in entrypoint
    assert "curl -LsSf \"$INSTALLER_URL\" | sh" in entrypoint


def test_neither_file_turns_certificate_verification_off() -> None:
    for name, source in _both_files().items():
        code = _code_only(source)
        for escape in VERIFICATION_ESCAPES:
            assert escape not in code, f"{name}: {escape}"


def test_every_proof_prints_a_verdict_line() -> None:
    """A run that says nothing is a run nobody can read the result of."""
    entrypoint = _entrypoint()

    assert "printf 'PASS: %s\\n'" in entrypoint, entrypoint
    assert "printf 'FAIL: %s\\n'" in entrypoint, entrypoint
    for label in ('"seed, ', '"proof 1, ', '"proof 2, '):
        assert label in entrypoint, label


def test_the_readme_records_the_retry_landed_and_the_container_proof_waits_on_a_release() -> None:
    """#326 landed the upgrade retry; the container gains its proof once a release seeds it.

    The retry the installer already made is now in `agentic-hil upgrade` too, so
    the README must not still promise it as work `agentic-hil upgrade` "does not
    know yet". What stays genuinely pending is a release carrying the retry for
    this bench to seed, after which the eval gains its third proof; the README
    names #326 and points at the unit test that pins the retry in the meantime.
    """
    readme = README.read_text(encoding="utf-8")

    assert "#326" in readme, readme
    assert "does not know it yet" not in readme, readme
    assert "test_upgrade_certificates.py" in readme, readme


def test_no_workflow_runs_this_eval_by_default() -> None:
    """Network and minutes. Default CI must not opt itself in.

    Both spellings of the extension, because a workflow added as `.yaml` is a
    workflow this would otherwise never read.
    """
    workflows = REPOSITORY_ROOT / ".github" / "workflows"
    for workflow in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        assert OPT_IN_VARIABLE not in workflow.read_text(encoding="utf-8"), workflow.name


def test_the_entrypoint_parses_in_a_posix_shell() -> None:
    shell = _posix_shell()

    result = subprocess.run(
        [shell, "-n", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_the_container_reproduces_both_halves_of_the_bench() -> None:
    """The eval itself: build the bench, run it, read its verdicts.

    Nothing here re-implements the assertions; the container makes them and says
    so line by line, and this asserts that it said all three and that the
    certificate error the bench reported is in the transcript verbatim. The
    container is removed by `--rm` whether it passed or failed.
    """
    docker = _docker_and_opt_in()

    build = subprocess.run(
        [docker, "build", "--file", str(DOCKERFILE), "--tag", IMAGE, str(REPOSITORY_ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=BUILD_TIMEOUT_S,
        check=False,
    )
    assert build.returncode == 0, f"{build.stdout}{build.stderr}"

    run = subprocess.run(
        [docker, "run", "--rm", IMAGE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CONTAINER_TIMEOUT_S,
        check=False,
    )

    transcript = f"{run.stdout}{run.stderr}"
    assert run.returncode == 0, transcript
    assert "PASS: seed," in transcript, transcript
    assert "PASS: proof 1," in transcript, transcript
    assert "PASS: proof 2," in transcript, transcript
    assert "invalid peer certificate: UnknownIssuer" in transcript, transcript
