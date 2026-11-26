"""Replay the suite-reduction witnesses on isolated working and base copies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from support import scaled_time_bound

ROOT = Path(__file__).resolve().parents[1]
INVENTORIES = Path("tests/fixtures/suite_reduction")
CACHES = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv"}
WORKERS_PER_REVISION = min(8, max(1, (os.cpu_count() or 2) // 2))


def clusters_for(slice_name: str | None) -> list[dict]:
    clusters = []
    for path in sorted((ROOT / INVENTORIES).glob("*.json")):
        if path.name == "protected_tests.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if slice_name is None or record["slice"] == slice_name:
            clusters.extend(record["clusters"])
    return clusters


def copy_working_tree(target: Path, scratch: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in CACHES or (Path(directory) / name).resolve() == scratch}

    shutil.copytree(ROOT, target, ignore=ignore)


def archive_base(base: str, target: Path) -> None:
    archive = target.with_suffix(".tar")
    subprocess.run(
        ["git", "archive", "--format=tar", "--output", str(archive), base],
        cwd=ROOT,
        check=True,
    )
    with tarfile.open(archive, "r:") as source:
        source.extractall(target, filter="data")
    archive.unlink()


def run_witnesses(
    tree: Path, nodes: Iterable[str], *, timeout: int = 90
) -> tuple[subprocess.CompletedProcess[str], list[ET.Element]]:
    report = tree.parent / "j.xml"
    report.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(tree / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        GIT_CEILING_DIRECTORIES=str(tree.parent),
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
            "-o", "junit_family=xunit1", "--junitxml", str(report),
            "--basetemp", str(tree.parent / "t"), *nodes,
        ],
        cwd=tree,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=scaled_time_bound(timeout),
    )
    return result, ET.parse(report).findall(".//testcase") if report.is_file() else []


def reports_node(case: ET.Element, node: str) -> bool:
    path, _, selection = node.partition("::")
    function, bracket, parameter = selection.partition("[")
    *classes, name = function.split("::")
    module = ".".join(Path(path).with_suffix("").parts)
    return (
        case.get("file", "").replace("\\", "/") == path
        and case.get("classname") == ".".join((module, *classes))
        and case.get("name") == name + bracket + parameter
    )


def replay(tree: Path, defect: dict, witness: str) -> str | None:
    source = (tree / defect["file"]).resolve()
    if not source.is_relative_to(tree) or not source.is_file():
        return f"missing mutation file {defect['file']}"
    before = source.read_bytes()
    # Match newline conventions in both git archives and local checkouts.
    newline = "\r\n" if b"\r\n" in before else "\n"
    find = defect["find"].replace("\n", newline).encode("utf-8")
    replacement = defect["replace"].replace("\n", newline).encode("utf-8")
    matches = before.count(find)
    if matches != 1:
        return f"stale find in {defect['file']} ({matches} matches)"
    try:
        source.write_bytes(before.replace(find, replacement, 1))
        result, cases = run_witnesses(tree, [witness])
        if (
            result.returncode == 1
            and len(cases) == 1
            and reports_node(cases[0], witness)
            and cases[0].find("failure") is not None
            and cases[0].find("error") is None
        ):
            return None
        return f"{witness} did not fail in its test call (pytest exit {result.returncode})\n{result.stdout}{result.stderr}"
    finally:
        source.write_bytes(before)


def on_revision(
    tree: Path, clusters: list[dict], witness_key: str, results: dict[str, list[str]],
) -> None:
    witnesses: dict[tuple[str, str, str, str], list[str]] = {}
    for cluster in clusters:
        for defect in cluster["defects"]:
            mutation = (defect["file"], defect["find"], defect["replace"], defect[witness_key])
            witnesses.setdefault(mutation, []).append(cluster["id"])
    jobs = list(witnesses.items())
    nodes = sorted({node for _, _, _, node in witnesses})
    if not nodes:
        return
    try:
        result, cases = run_witnesses(tree, nodes, timeout=240)
    except (OSError, subprocess.TimeoutExpired, ET.ParseError) as error:
        for cluster in clusters:
            results[cluster["id"]].append(f"{witness_key} pristine witnesses could not run: {type(error).__name__}")
        return
    missing = [
        node for node in nodes if not any(
            reports_node(case, node) and all(case.find(tag) is None for tag in ("failure", "error", "skipped"))
            for case in cases
        )
    ]
    if result.returncode != 0 or missing:
        for cluster in clusters:
            results[cluster["id"]].append(f"{witness_key} pristine witnesses did not pass")
        if missing:
            print(f"Missing green {witness_key} nodes:\n" + "\n".join(missing))
        print(result.stdout + result.stderr)
        return

    # Each worker gets its own source tree, so mutations never overlap.
    worker_count = min(WORKERS_PER_REVISION, len(jobs))

    def replay_batch(index: int) -> list[tuple[str, str]]:
        copy = tree.parent / str(index) / "r"
        shutil.copytree(tree, copy)
        failures = []
        for (file, find, replacement, node), cluster_ids in jobs[index::worker_count]:
            try:
                problem = replay(copy, {"file": file, "find": find, "replace": replacement}, node)
            except (OSError, subprocess.TimeoutExpired, ET.ParseError) as error:
                problem = str(error)
            if problem is not None:
                failures.extend((cluster_id, f"{witness_key}: {problem}") for cluster_id in cluster_ids)
        return failures

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        for failures in pool.map(replay_batch, range(worker_count)):
            for cluster_id, failure in failures:
                results[cluster_id].append(failure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--base")
    parser.add_argument("--slice", dest="slice_name")
    arguments = parser.parse_args()
    clusters = clusters_for(arguments.slice_name)
    if not clusters:
        parser.error("no clusters match the selected slice")
    results: dict[str, list[str]] = {cluster["id"]: [] for cluster in clusters}
    scratch = arguments.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s", dir=scratch) as directory:
        root = Path(directory).resolve()
        working = root / "a" / "r"
        base = root / "b" / "r"
        copy_working_tree(working, root)
        if arguments.base:
            base.parent.mkdir()
            archive_base(arguments.base, base)
        if arguments.base:
            base_results: dict[str, list[str]] = {key: [] for key in results}
            with ThreadPoolExecutor(max_workers=2) as revisions:
                base_run = revisions.submit(on_revision, base, clusters, "caught_before", base_results)
                on_revision(working, clusters, "caught_after", results)
                base_run.result()
            for key, failures in base_results.items():
                results[key].extend(failures)
        else:
            on_revision(working, clusters, "caught_after", results)
    for cluster_id, failures in results.items():
        print(f"{cluster_id}: {'failed' if failures else 'caught'}")
        for failure in failures:
            print(f"  {failure}")
    return int(any(results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
