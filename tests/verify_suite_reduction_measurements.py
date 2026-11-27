"""Explicit checks for hosted measurement evidence and the pip-free command."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

from test_ci_workflow import worker_setting
from test_suite_reduction_selection import LEGS, ROOT, duration, read_inventory, static_exclusions

MANIFEST_VARIABLE = "SUITE_REDUCTION_MEASUREMENT_MANIFEST"
BASELINE = "d0373f4b821b499bf7a20a8f57919758100c10e6"
BASELINE_RUN = "45500560760"
PRE_D = "e0b1c828e9b92ec01c45843eeb553e1f764af097"
PRE_D_RUN = "45885126992"
HISTORICAL_DIGEST = "b9a4e3bb6639dcea4e747d5a791d02395f12a241f4f102fbff509f0c050b3816"
HISTORICAL_KEYS = {"before": "before", "after": "after_abc_before_d"}
MATRIX = {f"{system}/{version}" for system, version in LEGS}
PIP_EVIDENCE_VARIABLE = "SUITE_REDUCTION_PIP_EVIDENCE"
PIP_PYTHON_VARIABLE = "SUITE_REDUCTION_PIP_FREE_PYTHON"
GROUPING_MODULE = "tests/test_process_table_grouping.py"
NESTED_GROUPING = f"{GROUPING_MODULE}::test_two_workers_run_the_whole_group_on_one_of_them"
MISSING_PIP_COMMAND = "CONTRIBUTING.md is missing the executable pip-free pytest command"


def evidence_file(reference: str) -> Path:
    path = (ROOT / reference).resolve()
    assert path.is_relative_to(ROOT), f"Measurement evidence must be inside the worktree: {reference}"
    assert path.is_file(), f"Missing measurement evidence: {reference}"
    return path


def junit_measurements(report: bytes, modules: set[str], label: str) -> dict[str, tuple[int, float]]:
    class_paths = {module.removesuffix(".py").replace("/", "."): module for module in modules}
    xml = ET.fromstring(report)
    suites = [xml] if xml.tag == "testsuite" else xml.findall("testsuite")
    cases = list(xml.iter("testcase"))
    assert cases and len(cases) == sum(int(suite.attrib["tests"]) for suite in suites), (
        f"{label}: incomplete JUnit report"
    )

    seconds = defaultdict(list)
    for case in cases:
        module = case.get("file", "").replace("\\", "/")
        if module not in modules:
            classname = case.get("classname", "")
            while classname and classname not in class_paths:
                classname, _, _ = classname.rpartition(".")
            module = class_paths.get(classname, "")
        assert module in modules, f"{label}: unknown testcase module {case.attrib}"
        elapsed = float(case.attrib["time"])
        assert duration(elapsed), f"{label}: invalid testcase time {case.attrib}"
        seconds[module].append(elapsed)
    return {module: (len(times), math.fsum(times)) for module, times in seconds.items()}


def collection_nodes(transcript: str) -> list[str]:
    return [line for line in transcript.splitlines() if line.startswith("tests/") and "::" in line]


def measure_revision(source: dict, reports: set[Path]) -> tuple[dict, dict]:
    label = f"revision {source['revision']} run {source['timing_run']}"
    assert source["timing_run"] and source["discovery_source"], f"{label}: missing measurement provenance"
    modules = set(source["discovered_modules"])
    transcript = evidence_file(source["collection_source"]).read_text(encoding="utf-8-sig")
    nodes = collection_nodes(transcript)
    assert nodes and len(set(nodes)) == len(nodes), f"{label}: missing or duplicate collection node IDs"
    collected = Counter(node.partition("::")[0] for node in nodes)
    assert not collected.keys() - modules, f"{label}: collected an undiscovered module"
    counts = {module: collected[module] for module in sorted(modules)}
    assert counts == source["collection_counts"], f"{label}: counts differ from the collection transcript"
    assert source["legs"].keys() == MATRIX, f"{label}: expected a JUnit report for each hosted leg"
    measurements = {module: {} for module in counts}
    for leg, evidence in sorted(source["legs"].items()):
        location = f"{label} / {leg}"
        report = evidence_file(evidence["junit_report"])
        assert report not in reports, f"{location}: JUnit report reused"
        reports.add(report)
        contents = report.read_bytes()
        assert sha256(contents).hexdigest() == evidence["sha256"], f"{location}: JUnit digest differs"
        measured = junit_measurements(contents, modules, location)
        deselected = set(evidence.get("deselected_modules", []))
        assert evidence.get("selection_source"), f"{location}: missing selection provenance"
        assert not deselected - modules, f"{location}: deselection names an undiscovered module"
        for module, count in counts.items():
            selected = 0 if module in deselected else count
            reported, seconds = measured.get(module, (0, 0.0))
            assert reported == selected, (
                f"{location} / {module}: expected {selected} selected testcases, JUnit reported {reported}"
            )
            measurements[module][leg] = "deselected" if module in deselected else seconds
    return counts, measurements


def manifest_bytes() -> bytes:
    reference = os.environ.get(MANIFEST_VARIABLE)
    assert reference, f"{MANIFEST_VARIABLE} must name the staged measurement manifest"
    return evidence_file(reference).read_bytes()


def historical_evidence() -> dict:
    contents = manifest_bytes()
    assert sha256(contents).hexdigest() == HISTORICAL_DIGEST, "Historical measurement manifest digest differs"
    evidence = json.loads(contents)["measurements"]
    for key, revision, run in (("before", BASELINE, BASELINE_RUN), ("after_abc_before_d", PRE_D, PRE_D_RUN)):
        assert (evidence[key]["revision"], evidence[key]["timing_run"]) == (revision, run), key
    return evidence


def revision_metadata(source: dict) -> dict:
    return {key: value for key, value in source.items() if key != "collection_counts"}


def test_inventory_matches_before_45500560760_and_after_abc_before_d_45885126992() -> None:
    evidence = historical_evidence()
    inventory = read_inventory("repository_tooling")
    rows = {row["module"]: row for row in inventory["modules"] + inventory["measurement_supplement"]}
    reports = set()
    for side, key in HISTORICAL_KEYS.items():
        source = evidence[key]
        counts, measurements = measure_revision(source, reports)
        assert revision_metadata(source) == inventory["measurements"][key], f"{key}: historical provenance differs"
        assert counts.keys() <= rows.keys(), f"{key}: discovered modules are missing from the inventory"
        for module, row in rows.items():
            label = f"{key} / {module}"
            if module in counts:
                assert row[f"tests_{side}"] == counts[module], f"{label}: collection count differs"
                assert row[f"legs_{side}"] == measurements[module], f"{label}: leg measurements differ from JUnit"
            else:
                assert row.get(f"status_{side}") == "absent", f"{label}: inventory must record historical absence"


def test_published_d_head_matches_hosted_collection_and_timings() -> None:
    evidence = json.loads(manifest_bytes())["measurements"]
    assert "published_d_head" in evidence, "Missing hosted measurement evidence for the published Slice D head"
    source = evidence["published_d_head"]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert head == source["revision"] and head not in {BASELINE, PRE_D}, "Measurements must match the published head"
    metadata = json.loads(evidence_file(source["job_metadata"]).read_text(encoding="utf-8-sig"))
    jobs = {job["name"]: job for job in metadata["jobs"]}
    for system, version in sorted(LEGS):
        leg = f"{system}/{version}"
        job = jobs[f"Python {version} on {system}"]
        assert job["head_sha"] == head and str(job["run_id"]) == source["timing_run"], leg
        assert any(step["name"] == "Run tests" and step["conclusion"] == "success" for step in job["steps"]), leg
        assert set(source["legs"][leg].get("deselected_modules", [])) == (
            {"tests/test_tool_definition_score_model.py"} | static_exclusions(version)
        ), f"{leg}: hosted selection differs from the intended selection"
    measure_revision(source, set())


def documented_pytest_arguments(document: str) -> list[str]:
    commands = []
    for block in re.finditer(r"(?m)^```[^\n]*\n(.*?)^```[ \t]*$", document, re.DOTALL):
        section = re.split(r"(?m)^#{1,6} ", document[:block.start()])[-1]
        if not re.search(r"pip-free|without pip|pip[- ]absent", section, re.IGNORECASE):
            continue
        for line in block[1].splitlines():
            if re.match(r"\s*(?:pytest\b|python(?:3(?:\.\d+)?)?\s+-m\s+pytest\b)", line):
                argv = shlex.split(line)
                commands.append(argv[1:] if argv[0] == "pytest" else argv[3:])
    if not commands:
        raise AssertionError(MISSING_PIP_COMMAND)
    assert len(commands) == 1, "CONTRIBUTING.md must identify one pip-free pytest command"
    return commands[0]


def junit_outcomes(report: Path, nodes: set[str]) -> dict[str, tuple[str, str]]:
    index = {}
    for node in nodes:
        module, _, qualified = node.partition("::")
        function, bracket, parameter = qualified.partition("[")
        *classes, name = function.split("::")
        classname = ".".join([module.removesuffix(".py").replace("/", "."), *classes])
        index[classname, name + bracket + parameter] = node

    outcomes = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        classname, name = case.attrib["classname"], case.attrib["name"]
        # xdist may append the worker group to the JUnit testcase name.
        node = index.get((classname, name)) or index.get((classname, name.rpartition("@")[0]))
        assert node is not None and node not in outcomes, f"{report.name}: unexpected testcase {classname}::{name}"
        verdict = next((entry for entry in case if entry.tag in {"failure", "error", "skipped"}), None)
        outcomes[node] = ("passed", "") if verdict is None else (verdict.tag, verdict.get("message", ""))
    assert outcomes.keys() == nodes, f"{report.name}: JUnit cases do not cover the specified collection"
    return outcomes


def paired_pip_selection() -> tuple[dict[str, tuple[str, str]], str]:
    reference = os.environ.get(PIP_EVIDENCE_VARIABLE)
    assert reference, f"{PIP_EVIDENCE_VARIABLE} must bind verified #834 and paired full-suite evidence"
    evidence = json.loads(evidence_file(reference).read_bytes())
    issue = json.loads(evidence_file(evidence["issue_source"]).read_bytes())
    assert issue["number"] == 834 and GROUPING_MODULE in issue["body"], "Missing verified #834 scope"
    present, absent = evidence["pip_present"], evidence["pip_absent"]
    assert present["pip_present"] is True and absent["pip_present"] is False
    assert present["interpreter"] and absent["interpreter"], "Missing paired interpreter identities"
    for key in ("revision", "environment_except_pip"):
        assert present[key] and present[key] == absent[key], f"Paired full-suite evidence differs on {key}"
    assert all(worker_setting(run["pytest_args"]) == "auto" for run in (present, absent)), (
        "#834 requires the parallel suite for the paired measurement"
    )

    paired_nodes = [set(collection_nodes(evidence_file(run["collection_source"]).read_text(encoding="utf-8-sig")))
                    for run in (present, absent)]
    assert paired_nodes[0] and paired_nodes[0] == paired_nodes[1], "#834 requires the same full-suite selection"
    nodes = paired_nodes[0]
    with_pip, without_pip = (junit_outcomes(evidence_file(run["junit_report"]), nodes) for run in (present, absent))
    assert all(outcome == "passed" or (outcome == "skipped" and reason)
               for outcomes in (with_pip, without_pip) for outcome, reason in outcomes.values())
    assert with_pip.get(NESTED_GROUPING) == without_pip.get(NESTED_GROUPING) == ("passed", ""), (
        "Missing nested grouping proof"
    )
    witnesses = evidence["passing_behavior_witnesses"]
    for node, witness in witnesses.items():
        assert with_pip[node][0] == without_pip[node][0] == "passed"
        assert witness["pip_present"] and witness["pip_absent"] and witness["pip_present"] != witness["pip_absent"]
        evidence_file(witness["source"])
    required = {NESTED_GROUPING} | witnesses.keys() | {node for node in nodes if with_pip[node] != without_pip[node]}
    return {node: without_pip[node] for node in required}, absent["interpreter"]


def test_paired_full_suite_evidence_covers_verified_issue_834() -> None:
    paired_pip_selection()


def test_documented_pip_free_command_reproduces_verified_issue_834_selection(tmp_path: Path) -> None:
    args = documented_pytest_arguments((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"))
    required, recorded_python = paired_pip_selection()
    python = os.environ.get(PIP_PYTHON_VARIABLE)
    assert python, f"{PIP_PYTHON_VARIABLE} must bind the verified pip-free interpreter"
    assert Path(python) == Path(recorded_python), "The bound interpreter differs from the paired pip-free run"
    observed = subprocess.run(
        [python, "-c", "import importlib.util; assert importlib.util.find_spec('pip') is None"], cwd=ROOT, check=False
    )
    assert observed.returncode == 0, "The bound interpreter must run Python without pip"
    argv = [python, "-m", "pytest", *args]
    collection = subprocess.run([*argv, "--collect-only", "-q"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert collection.returncode == 0, collection.stdout + collection.stderr
    assert set(collection_nodes(collection.stdout)) == required.keys(), "Documented pip-free collection differs"
    report = tmp_path / "pip-free.xml"
    reporting = shlex.quote(f"--junitxml={report}")
    addopts = f"{os.environ.get('PYTEST_ADDOPTS', '')} {reporting}"
    execution = subprocess.run(argv, cwd=ROOT, env={**os.environ, "PYTEST_ADDOPTS": addopts}, check=False)
    assert execution.returncode == 0, "The documented pip-free command failed"
    assert junit_outcomes(report, set(required)) == required, "Documented pip-free outcomes differ from paired evidence"
