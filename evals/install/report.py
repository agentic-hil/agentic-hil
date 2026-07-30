from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

MAX_DETAIL_CHARS = 240


def load_results(output_root: Path | str) -> list[dict[str, Any]]:
    """Read every per-run result document under an evaluation output directory."""
    root = Path(output_root)
    if not root.is_dir():
        raise ValueError(f"evaluation output directory does not exist: {root}")

    results = []
    for path in sorted(root.glob("*/result.json")):
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            raise ValueError(f"result document is not an object: {path}")
        results.append(document)
    if not results:
        raise ValueError(f"no run results were found under {root}")
    return results


def result_group(result: dict[str, Any]) -> tuple[str, str, str, str]:
    """The case, agent CLI, model, and effort whose repetitions must agree.

    The effort belongs in the key. Without it, one passing high-effort run and
    one failing low-effort run of the same model look like repetitions that
    disagreed, and the matrix spends its escalation budget chasing a difference
    that is the measurement itself.
    """
    case = result.get("case") if isinstance(result.get("case"), dict) else {}
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    return (
        str(case.get("id", "unknown-case")),
        str(agent.get("cli", "unknown-agent")),
        str(agent.get("model", "unknown-model")),
        str(agent.get("reasoning_effort") or "default"),
    )


def group_statuses(results: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[str]]:
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    for result in results:
        grouped.setdefault(result_group(result), []).append(str(result.get("status", "unknown")))
    return grouped


def unstable_groups(results: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """Combinations whose repetitions disagreed with each other.

    One passing and one failing run of the same combination means the pass was
    luck or the failure was noise. Neither is a result, so the matrix repeats
    that combination until it agrees with itself or hits its ceiling.
    """
    return sorted(group for group, statuses in group_statuses(results).items() if len(set(statuses)) > 1)


def measured_seconds(results: list[dict[str, Any]]) -> dict[str, float]:
    """What each agent and model cost, keyed as "<cli>/<model>".

    Only useful for deciding what to start first. A run that errored is included
    on purpose: a combination that stalls is expensive whether or not it produced
    a verdict, and starting it late is exactly what makes everyone else wait.
    """
    seconds: dict[str, list[float]] = {}
    for result in results:
        agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
        duration = result.get("duration_seconds")
        if not isinstance(duration, (int, float)):
            continue
        key = f"{agent.get('cli', 'unknown')}/{agent.get('model', 'unknown')}"
        seconds.setdefault(key, []).append(float(duration))
    return {key: round(sum(values) / len(values), 1) for key, values in sorted(seconds.items())}


def failed_checks(result: dict[str, Any]) -> list[dict[str, Any]]:
    checks = result.get("checks")
    if not isinstance(checks, list):
        return []
    return [check for check in checks if isinstance(check, dict) and not check.get("ok")]


def _one_line(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    collapsed = " ".join(str(text).split())
    if len(collapsed) > MAX_DETAIL_CHARS:
        return f"{collapsed[:MAX_DETAIL_CHARS]} ..."
    return collapsed


def _pass_rate(results: list[dict[str, Any]]) -> str:
    passed = sum(1 for result in results if result.get("status") == "passed")
    return f"{passed}/{len(results)}"


def _grouped_rates(results: list[dict[str, Any]], key: str, field: str) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        section = result.get(key)
        name = str(section.get(field)) if isinstance(section, dict) else "unknown"
        groups.setdefault(name, []).append(result)
    return [f"  {name}: {_pass_rate(rows)}" for name, rows in sorted(groups.items())]


def format_report(results: list[dict[str, Any]], output_root: Path | str) -> str:
    """Render one readable page: every run, why it failed, and the pass rates."""
    root = Path(output_root)
    lines = [f"Installation evaluation report: {root}", ""]

    for result in results:
        agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
        case = result.get("case") if isinstance(result.get("case"), dict) else {}
        duration = result.get("duration_seconds")
        # Whole seconds make two distinct repetitions look like one duplicated run.
        elapsed = f" in {duration:.1f}s" if isinstance(duration, (int, float)) else ""
        status = str(result.get("status", "unknown")).upper()
        case_id = case.get("id", "unknown-case")
        cli = agent.get("cli", "unknown-agent")
        model = agent.get("model", "unknown-model")
        lines.append(f"[{status}] {case_id} | {cli} {model}{elapsed}")
        for check in failed_checks(result):
            lines.append(f"    failed check: {check.get('name')} :: {_one_line(check.get('detail'))}")
        if result.get("failure"):
            lines.append(f"    failure: {_one_line(result['failure'])}")
        if result.get("status") != "passed" and result.get("id"):
            lines.append(f"    transcript: {root / str(result['id']) / 'agent.log'}")

    lines.extend(["", f"Pass rate: {_pass_rate(results)}", "", "By case:"])
    lines.extend(_grouped_rates(results, "case", "id"))
    lines.extend(["", "By agent:"])
    lines.extend(_grouped_rates(results, "agent", "cli"))

    statuses = group_statuses(results)
    unstable = unstable_groups(results)
    if unstable:
        lines.extend(["", "Unstable — repetitions disagreed:"])
        for group in unstable:
            case_id, cli, model, effort = group
            counted = ", ".join(
                f"{count}x {status}"
                for status, count in sorted(Counter(statuses[group]).items())
            )
            lines.append(f"  {case_id} | {cli} {model} ({effort}): {counted}")
    return "\n".join(lines)


def report_results(args: argparse.Namespace) -> int:
    results = load_results(args.output)
    print(format_report(results, args.output))
    return 0 if all(result.get("status") == "passed" for result in results) else 1


def timings_results(args: argparse.Namespace) -> int:
    """Print what each combination cost, to order the next run longest-first."""
    print(json.dumps(measured_seconds(load_results(args.output)), indent=2, sort_keys=True))
    return 0
