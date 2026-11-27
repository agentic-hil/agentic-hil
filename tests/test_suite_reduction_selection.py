"""Check the suite reduction's joined measurements and hosted selections."""

from __future__ import annotations

import json
import math
import re
import shlex
from itertools import product
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
INVENTORIES = ROOT / "tests/fixtures/suite_reduction"
EARLIER_SLICES = ("definitions_and_catalogue", "slow_tests", "parameter_tables")
OPERATING_SYSTEMS = ("ubuntu-latest", "macos-latest", "windows-latest")
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14", "3.15", "3.x")
LEGS = {(os, version) for os in OPERATING_SYSTEMS for version in PYTHON_VERSIONS}
STATIC_MODULES = {"tests/test_codeql_encoding_declaration.py", "tests/test_prose_convention.py"}
SELECTION_FAILURE = (
    "Hosted CI must select both static repository checks on the required Python 3.13 leg only for each operating system"
)


def discovered_modules(config: pytest.Config) -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for directory in config.getini("testpaths")
        for pattern in config.getini("python_files")
        for path in (ROOT / directory).rglob(pattern)
        if path.is_file()
    }


def read_inventory(name: str) -> dict:
    return json.loads((INVENTORIES / f"{name}.json").read_text(encoding="utf-8"))


def duration(value: object) -> bool:
    if type(value) is int:
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def static_exclusions(version: str) -> set[str]:
    return set() if version == "3.13" else STATIC_MODULES


def measurement_problems(row: dict, revision: dict, side: str, legs: set[str]) -> list[str]:
    module = row["module"]
    location = f"{module} / {side} revision {revision.get('revision') or '<missing>'}"
    present = module in revision.get("discovered_modules", [])
    if not present:
        # Discovery for this revision is the evidence for the explicit absence.
        if (
            row.get(f"status_{side}") != "absent"
            or not revision.get("discovery_source")
            or row.get(f"tests_{side}") is not None
            or row.get(f"seconds_{side}") is not None
            or row.get(f"legs_{side}")
        ):
            return [f"{location} / discovery / absence evidence"]
        return []

    problems = []
    for field in ("revision", "discovery_source", "collection_source", "timing_run"):
        if not isinstance(revision.get(field), str) or not revision[field].strip():
            problems.append(f"{location} / all legs / {field}")
    count = row.get(f"tests_{side}")
    if type(count) is not int or count < 0:
        problems.append(f"{location} / collection / tests_{side}")

    recorded = row.get(f"legs_{side}") or {}
    reports = revision.get("legs") or {}
    measured = []
    for leg in sorted(legs):
        report = reports.get(leg) or {}
        if not report.get("junit_report"):
            problems.append(f"{location} / {leg} / junit_report")
        seconds = recorded.get(leg)
        deselected = module in report.get("deselected_modules", [])
        if deselected:
            if seconds != "deselected" or not report.get("selection_source"):
                problems.append(f"{location} / {leg} / deselection evidence")
        elif not duration(seconds):
            problems.append(f"{location} / {leg} / seconds_{side}")
        else:
            measured.append(seconds)

    total = row.get(f"seconds_{side}")
    if not duration(total) or (not problems and not math.isclose(total, sum(measured))):
        problems.append(f"{location} / all legs / seconds_{side}")
    return problems


def test_joined_inventory_covers_discovered_modules_and_measured_legs(pytestconfig: pytest.Config) -> None:
    if not WORKFLOW.is_file():
        pytest.skip("suite reduction inventory covers the repository checkout")
    discovered = discovered_modules(pytestconfig)
    earlier = {row["module"] for name in EARLIER_SLICES for row in read_inventory(name)["modules"]}
    inventory = read_inventory("repository_tooling")
    slice_d = inventory["modules"]
    slice_d_paths = {row["module"] for row in slice_d}
    expected_d = discovered - earlier
    assert slice_d_paths == expected_d, (
        f"Slice D missing {sorted(expected_d - slice_d_paths)}, extra {sorted(slice_d_paths - expected_d)}"
    )

    measurements = inventory["measurements"]
    before, after = measurements["before"], measurements["after_abc_before_d"]
    assert before["revision"] == inventory["base_commit"]
    paths = set(before["discovered_modules"]) | set(after["discovered_modules"]) | discovered
    all_rows = slice_d + inventory["measurement_supplement"]
    joined = {row["module"]: row for row in all_rows}
    assert len(joined) == len(all_rows), "Duplicate joined measurement rows"
    assert joined.keys() == paths, (
        f"Joined inventory missing {sorted(paths - joined.keys())}, extra {sorted(joined.keys() - paths)}"
    )

    legs = {f"{os}/{version}" for os, version in LEGS}
    problems = [
        problem
        for module in sorted(paths)
        for side, revision in (("before", before), ("after", after))
        for problem in measurement_problems(joined[module], revision, side, legs)
    ]
    assert not problems, f"{len(problems)} incomplete measurements. First 30:\n" + "\n".join(problems[:30])


def test_missing_duration_names_module_revision_leg_and_field() -> None:
    inventory = read_inventory("repository_tooling")
    revision = inventory["measurements"]["after_abc_before_d"]
    leg = "windows-latest/3.13"
    legs = {f"{system}/{version}" for system, version in LEGS}
    row = next(row for row in inventory["modules"] if duration(row.get("legs_after", {}).get(leg)))
    assert measurement_problems(row, revision, "after", legs) == []
    incomplete = {**row, "legs_after": {**row["legs_after"], leg: None}}
    assert measurement_problems(incomplete, revision, "after", legs) == [
        f"{row['module']} / after revision {revision['revision']} / {leg} / seconds_after"
    ]


def matrix_legs(matrix: dict) -> dict[tuple[str, str], dict]:
    axes = {key: values for key, values in matrix.items() if key not in {"include", "exclude"}}
    base = [dict(zip(axes, values, strict=True)) for values in product(*axes.values())]
    entries = {
        (leg["os"], leg["python-version"]): leg.copy()
        for leg in base
        if not any(item.items() <= leg.items() for item in matrix.get("exclude", []))
    }
    for include in matrix.get("include", []):
        constraints = {key: include[key] for key in axes.keys() & include.keys()}
        matches = [leg for leg in base if constraints.items() <= leg.items()]
        if matches:
            for leg in matches:
                entry = entries.get((leg["os"], leg["python-version"]))
                if entry is not None:
                    entry.update(include)
        else:
            key = include["os"], include["python-version"]
            if key in entries:
                raise ValueError(f"duplicate matrix leg {key}")
            entries[key] = include
    if len(base) != len({(leg["os"], leg["python-version"]) for leg in base}):
        raise ValueError("multiple hosted legs share one operating system and Python version")
    return entries


def workflow_value(expression: str, matrix: dict, env: dict) -> object:
    """Resolve matrix values and the conditional strings used in pytest arguments."""
    conditional = re.fullmatch(r"(.+?)\s*(&&|\|\|)\s*('(?:[^']*)'|false|true)(?:\s*\|\|\s*('[^']*'))?", expression)
    if conditional:
        condition, operator, first, otherwise = conditional.groups()
        value = workflow_value(condition, matrix, env)
        if operator == "||":
            return value or workflow_value(first, matrix, env)
        selected = workflow_value(first, matrix, env) if value else value
        if otherwise is not None and not selected:
            return workflow_value(otherwise, matrix, env)
        return selected
    comparison = re.fullmatch(r"(matrix\.[\w-]+)\s*(==|!=)\s*('[^']*')", expression)
    if comparison:
        name, operator, literal = comparison.groups()
        equal = workflow_value(name, matrix, env) == workflow_value(literal, matrix, env)
        return equal if operator == "==" else not equal
    if expression.startswith("matrix."):
        return matrix.get(expression.removeprefix("matrix."), "")
    if expression.startswith("env."):
        return env.get(expression.removeprefix("env."), "")
    if expression == "runner.temp":
        return "runner-temp"
    if expression in {"true", "false"}:
        return expression == "true"
    if expression.startswith("'") and expression.endswith("'"):
        return expression[1:-1]
    raise ValueError(f"unresolved workflow expression {expression!r}")


def render(value: str, leg: dict, env: dict) -> str:
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: str(workflow_value(match[1], leg, env)), value)


def omitted_modules(arguments: list[str], full: set[str]) -> set[str]:
    """Resolve explicit module exclusions and reject other collection changes."""
    omitted = set()
    options = iter(arguments)
    for argument in options:
        option, delimiter, value = argument.partition("=")
        if option in {"-n", "--numprocesses", "--durations", "--basetemp", "--junitxml"}:
            if not delimiter:
                next(options)
        elif option in {"--ignore", "--deselect"}:
            path = PurePosixPath(value if delimiter else next(options)).as_posix()
            if path not in full:
                raise ValueError(f"exclusion is not an exact collected module path: {path}")
            omitted.add(path)
        else:
            raise ValueError(f"cannot establish full collection with pytest argument {argument!r}")
    return omitted


def test_hosted_matrix_selects_both_static_modules_once_per_os(pytestconfig: pytest.Config) -> None:
    if not WORKFLOW.is_file():
        pytest.skip("ci.yml is repository content and this checkout has none")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    steps = [step for step in job["steps"] if step.get("name") == "Run tests"]
    try:
        actual = matrix_legs(job["strategy"]["matrix"])
    except (KeyError, ValueError):
        raise AssertionError(SELECTION_FAILURE) from None
    full = discovered_modules(pytestconfig)
    if len(steps) != 1 or actual.keys() != LEGS or STATIC_MODULES - full:
        raise AssertionError(SELECTION_FAILURE)

    step = steps[0]
    variables = workflow.get("env", {}) | job.get("env", {}) | step.get("env", {})
    mismatches = []
    for key, leg in actual.items():
        _, version = key
        try:
            env = {key: render(str(value), leg, {}) for key, value in variables.items()}
            command = shlex.split(render(step["run"], leg, env))
            addopts = shlex.split(env.get("PYTEST_ADDOPTS", ""))
            allowed_failure = render(str(job.get("continue-on-error", "false")), leg, env).lower() == "true"
            selected = full - omitted_modules(addopts + command[1:], full)
            enabled = render(str(step.get("if", "true")), leg, env).lower() in {"true", "success()"}
        except (StopIteration, ValueError):
            mismatches.append(key)
            continue
        expected = full - static_exclusions(version)
        if not enabled or command[:1] != ["pytest"] or selected != expected or (version == "3.13" and allowed_failure):
            mismatches.append(key)
    if mismatches:
        raise AssertionError(SELECTION_FAILURE)
