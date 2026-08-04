from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from evals.install import runner
from evals.install.config import load_matrix
from evals.install.runner import expected_mcp_tools, job_payload
from evals.install.source import source_digest
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


def test_eval_image_allocates_an_unused_non_root_uid() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "useradd --create-home --user-group --shell /bin/bash eval" in dockerfile
    assert "--uid 1000" not in dockerfile
    assert "\nUSER eval\n" in dockerfile


# ---------------------------------------------------------------------------
# A published target is anchored to the release, fetched outside the container.


def _wheel_bytes(version: str, source: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("agentic_hil/__init__.py", source)
        bundle.writestr(f"agentic_hil-{version}.dist-info/METADATA", f"Name: agentic-hil\nVersion: {version}\n")
    return buffer.getvalue()


def _index_document(version: str, payload: bytes, **overrides: object) -> dict:
    artifact = {
        "packagetype": "bdist_wheel",
        "filename": f"agentic_hil-{version}-py3-none-any.whl",
        "url": f"https://files.pythonhosted.org/packages/ab/agentic_hil-{version}-py3-none-any.whl",
        "digests": {"sha256": hashlib.sha256(payload).hexdigest()},
        **overrides,
    }
    return {"urls": [artifact, {"packagetype": "sdist", "filename": f"agentic_hil-{version}.tar.gz"}]}


def _serve(monkeypatch: pytest.MonkeyPatch, document: dict, payload: bytes) -> list[str]:
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return json.dumps(document).encode("utf-8") if url.startswith("https://pypi.org/") else payload

    runner.published_release.cache_clear()
    monkeypatch.setattr(runner, "_fetch", fetch)
    return fetched


def test_a_published_target_is_digested_from_the_official_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The trust anchor the published arm had none of.

    Nothing on the host digests to a released wheel, so the wheel itself is
    fetched here — where the agent's container cannot reach it — checked
    against the index's own sha256, and digested exactly as the verifier
    digests the installed package tree."""
    payload = _wheel_bytes("1.2.3", "__version__ = '1.2.3'\n")
    fetched = _serve(monkeypatch, _index_document("1.2.3", payload), payload)

    release = runner.published_release("1.2.3")

    unpacked = tmp_path / "agentic_hil"
    unpacked.mkdir()
    (unpacked / "__init__.py").write_text("__version__ = '1.2.3'\n", encoding="utf-8")
    assert release["package_digest"] == source_digest(unpacked)
    assert release["sha256"] == hashlib.sha256(payload).hexdigest()
    assert fetched[0] == "https://pypi.org/pypi/agentic-hil/1.2.3/json"


def test_a_release_artifact_that_does_not_match_the_index_digest_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _wheel_bytes("1.2.3", "__version__ = '1.2.3'\n")
    document = _index_document("1.2.3", _wheel_bytes("1.2.3", "something else\n"))
    _serve(monkeypatch, document, payload)

    with pytest.raises(ValueError, match="does not match the sha256"):
        runner.published_release("1.2.3")


def test_a_release_artifact_from_another_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _wheel_bytes("1.2.3", "__version__ = '1.2.3'\n")
    document = _index_document("1.2.3", payload, url="https://example.invalid/agentic_hil-1.2.3-py3-none-any.whl")
    _serve(monkeypatch, document, payload)

    with pytest.raises(ValueError, match="files.pythonhosted.org"):
        runner.published_release("1.2.3")


def test_a_published_job_carries_the_release_digest_and_names_the_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    published = dataclasses.replace(
        matrix.target,
        mode="published",
        guide_url="https://raw.githubusercontent.com/agentic-hil/agentic-hil/main/AI_AGENT_QUICKSTART.md",
    )
    payload = _wheel_bytes(published.expected_version, "__version__ = 'x'\n")
    _serve(monkeypatch, _index_document(published.expected_version, payload), payload)

    payload_document = job_payload(
        dataclasses.replace(matrix, target=published),
        matrix.cases[0],
        matrix.jobs[0],
        None,
        REPOSITORY_ROOT,
    )

    target = payload_document["target"]
    assert re.fullmatch(r"[0-9a-f]{64}", target["expected_package_digest"])
    assert target["release_artifact"]["filename"].startswith(f"agentic_hil-{published.expected_version}-")
    assert target["release_artifact"]["url"].startswith("https://files.pythonhosted.org/")
