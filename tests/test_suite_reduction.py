"""Check that suite reductions name their surviving tests and defect witnesses."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORIES = Path("tests/fixtures/suite_reduction")
PROTECTED = "protected_tests.json"
HEADER_FIELDS = {"slice", "base_commit", "seconds_source", "modules", "clusters"}
MODULE_FIELDS = {"module", "tests_before", "tests_after", "seconds_before", "seconds_after"}
CLUSTER_FIELDS = {"id", "action", "nodes", "cases_before", "cases_after", "reason", "surviving_tests", "defects"}
DEFECT_FIELDS = {"file", "find", "replace", "caught_before", "caught_after"}


def function_names(body: list[ast.stmt], prefix: str = "") -> set[str]:
    names = set()
    for statement in body:
        if isinstance(statement, ast.ClassDef):
            names.update(function_names(statement.body, f"{prefix}{statement.name}::"))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name.startswith("test_"):
            names.add(prefix + statement.name)
    return names


def nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def count(value: object) -> bool:
    return type(value) is int and value >= 0


def node_path(node: object) -> tuple[str, str]:
    if not isinstance(node, str):
        return "", ""
    path, _, function = node.partition("::")
    return path, function.partition("[")[0]


class Inventory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.names = cache(self._names)

    def _names(self, path: str) -> set[str]:
        source = (self.root / path).resolve()
        if not path or not source.is_relative_to(self.root) or not source.is_file() or source.suffix != ".py":
            return set()
        return function_names(ast.parse(source.read_bytes(), filename=path).body)

    def exists(self, node: object) -> bool:
        path, function = node_path(node)
        return bool(function) and function in self.names(path)

    def slice_problems(self, record: dict) -> list[str]:
        if HEADER_FIELDS - record.keys():
            return ["missing inventory fields"]
        problems = []
        for row in record["modules"]:
            if MODULE_FIELDS - row.keys():
                problems.append("missing module fields")
                continue
            path = (self.root / row["module"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                problems.append(f"missing module {row['module']}")
        for cluster in record["clusters"]:
            problems.extend(self.cluster_problems(cluster))
        return problems

    def cluster_problems(self, cluster: dict) -> list[str]:
        missing = CLUSTER_FIELDS - cluster.keys()
        if missing:
            return [f"missing cluster fields {sorted(missing)}"]
        problems = []
        if not nonblank(cluster["reason"]):
            problems.append("missing reason")
        action = cluster["action"]
        before, after = cluster["cases_before"], cluster["cases_after"]
        if not (
            count(before) and count(after) and before >= after
            and (action == "cheaper" or (action in ("merged", "removed") and before > after))
        ):
            problems.append("case counts contradict action")
        if not cluster["nodes"] or not all(
            node_path(node)[1] and "[" not in node for node in cluster["nodes"]
        ):
            problems.append("missing or invalid base functions")
        if not cluster["surviving_tests"]:
            problems.append("missing survivor")
        for node in cluster["surviving_tests"]:
            if not self.exists(node) or "[" in node:
                problems.append(f"missing surviving function {node}")
        if not cluster["defects"]:
            problems.append("missing defect")
        for defect in cluster["defects"]:
            if DEFECT_FIELDS - defect.keys() or not all(
                nonblank(defect[key]) for key in DEFECT_FIELDS - {"replace"}
            ) or not isinstance(defect["replace"], str):
                problems.append("missing defect fields")
            elif not self.exists(defect["caught_after"]):
                problems.append(f"missing caught_after function {defect['caught_after']}")
        return [f"{cluster['id']}: {problem}" for problem in problems]

    def protection_problems(self, paths: dict[str, list[str]]) -> list[str]:
        return [
            f"missing protected function {path}::{name}"
            for path, names in sorted(paths.items())
            for name in names
            if name not in self.names(path)
        ]


def test_suite_reduction_inventory() -> None:
    inventory = Inventory(ROOT)
    directory = ROOT / INVENTORIES
    slices = sorted(path for path in directory.glob("*.json") if path.name != PROTECTED)
    assert slices, "suite reduction inventories are missing"
    problems = [
        f"{path.name}: {problem}"
        for path in slices
        for problem in inventory.slice_problems(json.loads(path.read_text(encoding="utf-8")))
    ]
    protected = json.loads((directory / PROTECTED).read_text(encoding="utf-8"))
    assert protected, "protected tests inventory is empty"
    problems.extend(inventory.protection_problems(protected))
    assert not problems, "\n".join(problems)


def test_suite_reduction_reports_invalid_records(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text(
        "class TestExample:\n    def test_kept(self):\n        pass\n", encoding="utf-8"
    )
    inventory = Inventory(tmp_path)
    node = "test_example.py::TestExample::test_kept"
    cluster = {
        "id": "example", "action": "merged", "nodes": [node], "cases_before": 2,
        "cases_after": 1, "reason": "same assertions", "surviving_tests": [node],
        "defects": [{"file": "product.py", "find": "claim", "replace": "",
                     "caught_before": f"{node}[first]", "caught_after": f"{node}[example]"}],
    }
    record = {
        "slice": "example", "base_commit": "base", "seconds_source": "sample",
        "modules": [{"module": "test_example.py", "tests_before": 2, "tests_after": 1,
                     "seconds_before": None, "seconds_after": None}],
        "clusters": [cluster],
    }
    assert not inventory.slice_problems(record)
    assert not inventory.protection_problems({"test_example.py": ["TestExample::test_kept"]})
    edits = [
        ({"reason": ""}, "missing reason"),
        ({"surviving_tests": []}, "missing survivor"),
        ({"defects": []}, "missing defect"),
        ({"cases_after": 2}, "case counts contradict action"),
        ({"surviving_tests": ["test_example.py::test_missing"]}, "missing surviving function"),
        ({"defects": [{**cluster["defects"][0], "caught_after": "test_example.py::test_missing"}]},
         "missing caught_after function"),
        ({"defects": [{}]}, "missing defect fields"),
    ]
    for edit, message in edits:
        assert any(message in problem for problem in inventory.cluster_problems({**cluster, **edit})), edit
    assert inventory.cluster_problems({key: value for key, value in cluster.items() if key != "reason"}) == [
        "missing cluster fields ['reason']"
    ]
    missing_module = deepcopy(record)
    missing_module["modules"][0]["module"] = "test_missing.py"
    assert inventory.slice_problems(missing_module) == ["missing module test_missing.py"]
    assert inventory.protection_problems({"test_example.py": ["TestExample::test_missing"]}) == [
        "missing protected function test_example.py::TestExample::test_missing"
    ]
