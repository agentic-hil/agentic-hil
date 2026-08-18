from __future__ import annotations

import ast
import dataclasses
import hashlib
import re
from dataclasses import fields
from pathlib import Path

import pytest

from agentic_hil.config import GENERATED_PROJECT_PERMISSIONS
from agentic_hil.knowledge import EXCLUSIVE_FLASH_PERMISSIONS
from agentic_hil.types import (
    CURRENT_CONFIG_VERSION,
    LEGACY_CONFIG_VERSION,
    READ_FREE_CONFIG_VERSION,
    SUPPORTED_CONFIG_VERSIONS,
    ProjectPermissions,
)
from evals.install import verifier
from evals.install.config import load_case, load_matrix
from evals.install.fixtures import HARDWARE_MAKE_TARGETS, MAKE_FLASH_FILES
from evals.install.runner import expected_mcp_tools, job_payload, source_version
from evals.install.verifier import target_mcp_tools

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_ENTRYPOINT = REPOSITORY_ROOT / "evals" / "install" / "container_entrypoint.py"


def _job_key_accesses(source: str) -> tuple[set[str], set[str]]:
    """Collect the ``job[...]`` and ``job["case"][...]`` keys a module reads."""
    top_level: set[str] = set()
    case: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Constant):
            continue
        key = node.slice.value
        if not isinstance(key, str):
            continue
        container = node.value
        if isinstance(container, ast.Name) and container.id == "job":
            top_level.add(key)
        elif (
            isinstance(container, ast.Subscript)
            and isinstance(container.value, ast.Name)
            and container.value.id == "job"
            and isinstance(container.slice, ast.Constant)
            and container.slice.value == "case"
        ):
            case.add(key)
    return top_level, case


def test_the_operator_answers_generically_rather_than_by_model() -> None:
    """Any agent that stops to ask gets the same answer, in the operator's voice.

    Measured across models: one asked for the guide it had never seen, another
    about login files it found under /run, another only about the scope. A reply
    written for one model's wording answers the next model not at all.
    """
    text = CONTAINER_ENTRYPOINT.read_text(encoding="utf-8")
    reply = re.search(r"OPERATOR_REPLY = \((.*?)\n\)", text, re.DOTALL)

    assert reply is not None
    body = reply.group(1)
    for named in ("sonnet", "claude", "codex", "opencode", "gpt"):
        assert named not in body.lower(), f"the operator's answer names {named}"
    for covered in ("{guide}", "user-level", "/run", "scope"):
        assert covered in body, f"the operator's answer does not address {covered}"
    assert re.search(r"for attempt in range\(1, MAX_OPERATOR_REPLIES \+ 1\)", text), "the reply is sent once only"


def test_container_entrypoint_reads_only_keys_the_runner_writes() -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    payload = job_payload(
        matrix,
        matrix.cases[0],
        matrix.jobs[0],
        REPOSITORY_ROOT,
        REPOSITORY_ROOT,
    )
    top_level, case = _job_key_accesses(CONTAINER_ENTRYPOINT.read_text(encoding="utf-8"))

    assert case, "the entrypoint must read the case description"
    assert "prompt_template" in case, "the prompt key must stay bound to the runner payload"
    assert top_level <= set(payload), sorted(top_level - set(payload))
    assert case <= set(payload["case"]), sorted(case - set(payload["case"]))
    # Pinned in both directions: the runner must write the effort and the
    # entrypoint must read it, or a level is requested and never applied.
    assert "reasoning_effort" in payload
    assert "reasoning_effort" in top_level


def test_every_agent_session_receives_the_reasoning_effort() -> None:
    """The measured session is the one the routing report reads.

    An effort that reached only the installing session would leave the
    follow-up answering at the CLI's default while the result claims the level
    that was asked for.
    """
    tree = ast.parse(CONTAINER_ENTRYPOINT.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_agent_command"
    ]

    assert len(calls) == 3, "one call per agent session: install, confirmation, follow-up"
    for call in calls:
        assert any(keyword.arg == "reasoning_effort" for keyword in call.keywords)


def test_the_payload_expects_what_the_install_produces_not_what_the_matrix_names() -> None:
    """The one field the runner overrides on its way into the container.

    A local run installs this tree, so the version every artifact reports is the
    tree's own. Between releases that is a development version while the matrix
    still names the release, and handing the matrix's number to the verifier
    would fail every check that reads a version, in every run, for a whole cycle.
    """
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    misnamed = dataclasses.replace(matrix, target=dataclasses.replace(matrix.target, expected_version="0.0.1"))

    payload = job_payload(misnamed, misnamed.cases[0], misnamed.jobs[0], REPOSITORY_ROOT, REPOSITORY_ROOT)

    assert payload["target"]["expected_version"] == source_version(REPOSITORY_ROOT)
    assert payload["target"]["expected_version"] != "0.0.1"


def _tagged_source_tree(root: Path, version: str) -> None:
    """A synthetic ``host_source_root`` this test process owns: everything
    `job_payload` reads for a published-mode job, tagged as a release.

    Built fresh under a tmp path rather than pointed at this repository's own
    checkout: a real checkout here is root-owned while the test process is
    not, and git's dubious-ownership guard refuses a `-C` command against it
    once the isolated per-test ``HOME`` fixture stops carrying this host's
    ``safe.directory`` exception.
    """
    import subprocess

    package = root / "src" / "agentic_hil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"__version__ = '1'\n")
    contract = root / "evals" / "install" / "tools.list.expected"
    contract.parent.mkdir(parents=True)
    contract.write_text("one_tool\ntwo_tools\n", encoding="utf-8")
    # The same pin this repository carries, so an archive of the tag hashes the
    # same as the working tree beside it regardless of core.autocrlf.
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    for command in (
        ["init"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "release"],
        ["tag", f"v{version}"],
    ):
        assert subprocess.run(["git", "-C", str(root), *command], capture_output=True).returncode == 0


def test_published_target_gets_a_real_digest_when_the_release_tag_is_present(tmp_path: Path) -> None:
    """Review finding: published mode used to hand the verifier a check nothing
    could ever satisfy, because the digest it was told to match was always
    None. When this clone carries the release tag, the payload must carry the
    same trusted reference `validate_source_matches_release` already trusts
    for local mode -- not None.
    """
    from evals.install.runner import committed_package_digest

    _tagged_source_tree(tmp_path, "0.4.0")
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    published = dataclasses.replace(
        matrix,
        target=dataclasses.replace(matrix.target, mode="published", expected_version="0.4.0"),
    )

    payload = job_payload(published, published.cases[0], published.jobs[0], None, tmp_path)

    assert payload["target"]["expected_package_digest"] is not None
    assert payload["target"]["expected_package_digest"] == committed_package_digest(tmp_path, "v0.4.0")


def test_published_target_without_the_release_tag_gets_no_digest_reference(tmp_path: Path) -> None:
    """The honest fallback: a clone that cannot answer for the release still
    names no reference, rather than one that would just never match."""
    _tagged_source_tree(tmp_path, "0.4.0")
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    published = dataclasses.replace(
        matrix,
        target=dataclasses.replace(matrix.target, mode="published", expected_version="999.999.999"),
    )

    payload = job_payload(published, published.cases[0], published.jobs[0], None, tmp_path)

    assert payload["target"]["expected_package_digest"] is None


def test_job_binds_mcp_contract_from_target_source() -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    names = expected_mcp_tools(REPOSITORY_ROOT)

    payload = job_payload(
        matrix,
        matrix.cases[0],
        matrix.jobs[0],
        REPOSITORY_ROOT,
        REPOSITORY_ROOT,
    )

    assert payload["target"]["expected_mcp_tools"] == names
    assert target_mcp_tools(payload["target"]) == set(names)


def test_target_mcp_contract_rejects_digest_mismatch() -> None:
    names = ["one_tool", "two_tools"]
    target = {
        "expected_mcp_tools": names,
        "expected_mcp_tools_digest": hashlib.sha256(b"different\n").hexdigest(),
    }

    with pytest.raises(ValueError, match="digest mismatch"):
        target_mcp_tools(target)


def test_source_mcp_contract_must_be_sorted_and_unique(tmp_path: Path) -> None:
    contract = tmp_path / "evals" / "install" / "tools.list.expected"
    contract.parent.mkdir(parents=True)
    contract.write_text("z_tool\na_tool\nz_tool\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sorted and unique"):
        expected_mcp_tools(tmp_path)


def test_eval_image_does_not_embed_current_checkout_tool_contract() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY evals/install/tools.list.expected" not in dockerfile


def test_every_case_prompt_names_where_the_package_comes_from() -> None:
    """A prompt handing only the guide sent every obedient agent to the index.

    The guide's own sentence is "A link to this guide is not that case; it means
    the current release", so in local mode an agent following it installs the
    released package instead of the tree under test. That was 117 of the 147
    failures in the last full matrix.
    """
    cases = sorted((REPOSITORY_ROOT / "evals" / "install" / "cases").glob("*.json"))

    assert cases
    for path in cases:
        case = load_case(path)
        assert "{install_spec}" in case.prompt_template, path.name
        assert "{guide}" in case.prompt_template, path.name


def test_the_guard_survives_a_login_shell() -> None:
    """`bash -lc` was blind to it: /etc/profile rewrites PATH from scratch.

    codex wraps its commands that way, and all 57 codex runs of the last full
    matrix could have reached the bench without the guard recording anything.
    Two answers, because either alone can be undone by the next thing that sets
    PATH.
    """
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    # Written, not merely mentioned: the redirect is what puts the drop-in there.
    assert 'ln -s /opt/evals/install/guard.py "/usr/local/bin/$(basename "$shim")"' in dockerfile
    assert "> /etc/profile.d/00-eval-guard.sh" in dockerfile
    assert 'PATH="/opt/eval-bench/bin:/opt/eval-guard/bin:$PATH"' in dockerfile


def test_the_bench_stand_in_is_what_a_bootstrap_adopts() -> None:
    """#222: `setup` adopts whatever `openocd` resolves to, and it must work.

    In published mode that was the guard, so every legitimate call the product
    made afterwards executed a program written to refuse it. The stand-in has to
    be ahead of the shims in both PATHs the container has, and it has to resolve
    to a file under the committed tree rather than to the guard's own script:
    the runtime pins an executable by its resolved path.
    """
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ln -s /opt/evals/install/bench_openocd.py /opt/eval-bench/bin/openocd" in dockerfile
    assert "chmod 0755 /opt/evals/install/bench_openocd.py" in dockerfile
    assert "PATH=/opt/eval-bench/bin:/opt/eval-guard/bin:" in dockerfile
    assert 'PATH="/opt/eval-bench/bin:/opt/eval-guard/bin:$PATH"' in dockerfile
    # A relative script name is what the generated template carries, so the
    # stand-in needs the tree the image ships to be on its search path.
    assert "OPENOCD_SCRIPTS=/usr/share/openocd/scripts" in dockerfile


def test_the_image_carries_the_openocd_scripts_a_generated_config_names() -> None:
    """Both containers come from this image, so a script tree here resolves in both.

    Without one, a configuration naming an OpenOCD script is refused as
    config_invalid by a verifier whose filesystem never had the file, about a
    bench that may have been sound where it was written.
    """
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    for script in ("interface/stlink.cfg", "target/stm32f4x.cfg"):
        assert f"/usr/share/openocd/scripts/{script}" in dockerfile
    # The same paths the workspace fixture's own openocd.cfg resolves through.
    assert "source [find interface/stlink.cfg]" in MAKE_FLASH_FILES["openocd.cfg"]
    assert "source [find target/stm32f4x.cfg]" in MAKE_FLASH_FILES["openocd.cfg"]


def test_the_make_targets_routing_reads_are_the_ones_the_fixture_offers() -> None:
    """A target named here that the Makefile does not drive hardware from is noise."""
    makefile = MAKE_FLASH_FILES["Makefile"]

    for target in HARDWARE_MAKE_TARGETS:
        assert f"\n{target}:" in makefile or f"\n{target}: " in makefile, target
    # `all` builds; it reaches no bench and must not be read as reaching one.
    assert "all" not in HARDWARE_MAKE_TARGETS


def test_eval_image_allocates_an_unused_non_root_uid() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "useradd --create-home --user-group --shell /bin/bash eval" in dockerfile
    assert "--uid 1000" not in dockerfile
    assert "\nUSER eval\n" in dockerfile


def test_the_verifier_permission_whitelist_is_the_one_this_release_generates() -> None:
    """The drift that stranded a full matrix, caught in the pull request instead.

    The verifier spells this list out rather than importing it, on purpose: it
    checks an installed distribution and must not read its expectations out of
    the thing it is verifying, or a release that both added a permission and
    wrote it would move the expectation along with itself. The price of spelling
    it out is that `allow_recover` shipped and every job in a full matrix failed
    on a config the install was right to write. This is where that price is
    paid instead: one release ahead of the eval, in the change that adds the
    permission, where the fix is one word.
    """
    assert verifier.PROJECT_PERMISSION_FLAGS == GENERATED_PROJECT_PERMISSIONS
    # Stated against the schema as well as against the generation: a permission
    # the type declares and no generation writes is still one this eval must
    # recognise in a file an operator edited.
    assert set(verifier.PROJECT_PERMISSION_FLAGS) == {field.name for field in fields(ProjectPermissions)}


def test_the_verifier_flash_interlock_pair_is_the_one_the_interlock_refuses_on() -> None:
    assert set(verifier.EXCLUSIVE_FLASH_PERMISSIONS) == set(EXCLUSIVE_FLASH_PERMISSIONS)


def test_the_verifier_reads_every_config_version_this_release_writes() -> None:
    """Version 3 was the second half of the same stranding, unread behind the first."""
    assert verifier.SUPPORTED_CONFIG_VERSIONS == SUPPORTED_CONFIG_VERSIONS
    assert verifier.LEGACY_CONFIG_VERSION == LEGACY_CONFIG_VERSION
    assert verifier.READ_FREE_CONFIG_VERSION == READ_FREE_CONFIG_VERSION
    # What an install writes today has to be inside the family the check treats
    # as read-free, or it demands the read permissions this model removed.
    assert CURRENT_CONFIG_VERSION >= verifier.READ_FREE_CONFIG_VERSION
    assert CURRENT_CONFIG_VERSION in verifier.SUPPORTED_CONFIG_VERSIONS


def test_the_wrong_workspace_probe_calls_a_tool_this_server_serves() -> None:
    """A renamed tool would leave the probe asking for nothing and reading a refusal into it."""
    contract = set(expected_mcp_tools(REPOSITORY_ROOT))

    assert verifier.UNPROVISIONED_PROBE_TOOL in contract
